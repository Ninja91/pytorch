# Copyright (c) Meta Platforms, Inc. and affiliates
import os
import warnings
from typing import List, Optional, Sequence, TypeVar, Union

import torch
from torch.distributed.distributed_c10d import (
    _get_default_group,
    all_to_all,
    broadcast,
    get_global_rank,
    get_rank,
    get_world_size,
    GroupMember,
    init_process_group,
    is_initialized,
    new_group,
    ProcessGroup,
    ReduceOp,
    scatter,
    Work,
)

import torch.distributed._functional_collectives as funcol

_global_device_mesh: Optional["DeviceMesh"] = None


def get_global_device_mesh() -> "DeviceMesh":
    global _global_device_mesh
    assert _global_device_mesh is not None, "Could not get a default device mesh!"
    return _global_device_mesh


def set_global_device_mesh(mesh: Optional["DeviceMesh"]) -> None:
    global _global_device_mesh
    _global_device_mesh = mesh


# We want a type for "can be passed to torch.as_tensor()";
# this is a recursive sequence type, which isn't fully supported
# yet in python. This construct simulates that up to depth 7.
T = TypeVar("T")
_L = Union[T, Sequence[T]]
NDIntList = _L[_L[_L[_L[_L[_L[_L[int]]]]]]]

MeshExprT = Union[
    torch.Tensor,
    NDIntList,
]

def _pad_tensor(tensor: torch.Tensor, pad_dim) -> torch.Tensor:
    # pad tensor by 1 on the shard dim
    pad = [0, 0] * (tensor.ndim - pad_dim)
    pad[-1] = 1
    return torch.nn.functional.pad(tensor, pad)

def _unpad_tensor(tensor: torch.Tensor, pad_dim) -> torch.Tensor:
    # unpad tensor by 1 on the shard dim
    return tensor.narrow(pad_dim, start=0, length=tensor.size(pad_dim) - 1)

class DeviceMesh:
    """
    DeviceMesh represents a mesh of devices, where layout of devices could be
    represented as a n-d dimension array, and each value of the n-d dimensional
    array is the global id of the default process group ranks.

    DeviceMesh could be used to describe the layout of devices across the cluster,
    and serves as a proxy for communication among the device lists within the cluster.

    We use the default ProcessGroup in this DeviceMesh class to implement proper
    communications. Note that we also add collective wrappers in this class. This is
    used to decouple detailed communication backend with the underlying
    DTensor implementation.

    DeviceMesh can be used as a context manager.
    Args:
        device_type (str): device type of the mesh. Currently supports: cpu, cuda.
        mesh (ndarray): could be a multi-dimension array or an integer tensor that
            describes the layout of devices, the ids are global ids of the
            default process group.
        dim_groups (List[ProcessGroup], optional): The ProcessGroup used per mesh
            dimension.

    Returns:
        A :class:`DeviceMesh` object

    Example (2 host with 4 GPUs each):
        ```
        # The following program runs on each process/rank in SPMD manner.
        # initialized default world
        torch.distributed.init_process_group(backend="nccl", world_size=8)
        # initialize device mesh as (2, 4) to represent the topology
        # of cross-host(dim 0), and within-host (dim 1)
        mesh = DeviceMesh(device_type="cuda",
                          mesh=[
                            [0, 1, 2, 3],
                            [4, 5, 6, 7]
                          ])
        ```
        A reduction over the first dimension of mesh will reduce across
        columns (0, 4), .. and (3, 7), a reduction over the second dimension
        of mesh reduces across rows (0, 1, 2, 3) and (4, 5, 6, 7)

    """

    device_type: str
    mesh: torch.Tensor
    _backend: str

    def __init__(
        self,
        device_type: str,
        mesh: MeshExprT,
        dim_groups: Optional[List[ProcessGroup]] = None,
    ) -> None:
        self.device_type = device_type
        self.mesh = (
            mesh.detach()
            if isinstance(mesh, torch.Tensor)
            else torch.tensor(mesh, dtype=torch.int)
        )
        default_pg = self._get_or_create_default_group()
        self._backend = default_pg._get_backend_name()
        # TODO: if user want to pass pg_options, offer a way to do it
        # check default pg backend, should support device_type
        if device_type == "cpu":
            assert (
                self._backend == "gloo" or self._backend == "threaded"
            ), f"ProcessGroup backend: {self._backend} not supporting CPU!"
        elif device_type == "cuda":
            if self._backend == "gloo":
                warnings.warn(
                    "We recommend using nccl backend for cuda device type, gloo backend might only have partial support!"
                )
            assert self._backend == "gloo" or self._backend == "nccl" or self._backend == "threaded"
        else:
            raise RuntimeError(
                f"DeviceMesh only support cpu or cuda device type, but got {device_type}"
            )

        world_size = get_world_size()
        if self.mesh.numel() > world_size:
            raise RuntimeError(
                f"Mesh should not be bigger than default world size, but found {self.mesh.numel()} ranks!"
            )

        unique_mesh_values = self.mesh.unique(sorted=True)
        if unique_mesh_values.numel() != self.mesh.numel():
            raise RuntimeError(
                f"DeviceMesh cannot have duplicate values, but found {self.mesh.tolist()}"
            )

        # coordinates of this rank on the mesh
        rank_coords = (self.mesh == get_rank()).nonzero()
        assert rank_coords.size(0) in (0, 1)
        self._coordinate_on_dim: Optional[List[int]] = (
            rank_coords[0].tolist() if rank_coords.size(0) > 0 else None
        )

        # groups created by dimension, each dimension should have exact
        # one valid process group per rank
        self._dim_groups: List[ProcessGroup] = []
        if dim_groups is not None:
            # if user hand creating dimension based groups
            # we just take it and use it for communication
            if not isinstance(dim_groups, list):
                raise RuntimeError(
                    "dim_groups expected to be Optional[List[ProcessGroup]]"
                )

            for group in dim_groups:
                if not isinstance(group, ProcessGroup):
                    raise RuntimeError(
                        f"found object in dim_groups that is not a ProcessGroup: {group}"
                    )

            if self.get_rank() in self.mesh:
                if len(dim_groups) != self.mesh.ndim:
                    raise RuntimeError(
                        f"length of dim_groups ({len(dim_groups)}) expected to be equal to mesh.ndim ({self.mesh.ndim})"
                    )
            else:
                if len(dim_groups) != 0:
                    raise RuntimeError(
                        f"length of dim_groups ({len(dim_groups)}) expected to be equal to 0 on rank {self.get_rank()} "
                        f"for mesh {self.mesh}"
                    )

            self._dim_groups = dim_groups
            return

        if self.mesh.ndim == 1 and unique_mesh_values[-1] == world_size - 1:
            # if the mesh is the same as world_pg, we just append the default
            # pg to the first dim goups, as new_group cannot have the exact
            # same ranks as world
            self._dim_groups.append(default_pg)
        else:
            # create sub pgs base on the mesh argument specified
            # handle multi-dim mesh, create subgroups by
            # looping over the pg_ranks_by_dim for each dim
            for dim in range(self.mesh.ndim):
                # swap the current dim to the last dim
                # then reshape to flatten out other dims
                pg_ranks_by_dim = self.mesh.swapdims(-1, dim).reshape(
                    -1, self.mesh.size(dim)
                )

                # multi-dim mesh, create subgroups by
                # looping over the pg_ranks for each dim
                # and append the groups
                for dim_mesh in pg_ranks_by_dim:
                    subgroup_ranks = dim_mesh.tolist()
                    # call new_group regardless of the current rank in the
                    # pg or not, it's required that all ranks participate
                    # in subgroup construction
                    new_subgroup = new_group(
                        ranks=subgroup_ranks, backend=self._backend
                    )
                    # only add to dim_groups if the current rank in the subgroup
                    if self.get_rank() in subgroup_ranks:
                        if len(self._dim_groups) > dim:
                            raise RuntimeError(
                                f"Each device mesh dimension should get only one process group, but got {self.get_rank} "
                                f"in {subgroup_ranks}!"
                            )
                        self._dim_groups.append(new_subgroup)

    def _get_or_create_default_group(self):
        if not is_initialized():
            # TODO: we will support mesh on a subset of WORLD in future
            world_size = int(os.getenv("WORLD_SIZE", 1))
            if self.mesh.numel() < world_size:
                raise RuntimeError(
                    "DeviceMesh must include every process in WORLD, "
                    f"but WORLD_SIZE({world_size}) != mesh size({self.mesh.numel()})"
                )

            unique_mesh_values = self.mesh.unique(sorted=True)
            if unique_mesh_values.numel() != self.mesh.numel():
                raise RuntimeError(
                    f"DeviceMesh cannot have duplicate values, but found {self.mesh.tolist()}"
                )

            # ranks in mesh must start from 0
            if unique_mesh_values[0] != 0:
                raise RuntimeError(
                    "DeviceMesh ranks must start from 0, "
                    f"but found min rank = {unique_mesh_values[0]}"
                )

            # mesh must be contiguous (i.e. from 0 to N-1)
            if 2 * unique_mesh_values.sum().item() != world_size * (world_size - 1):
                raise RuntimeError(
                    f"DeviceMesh should have all ranks of WORLD, but found {self.mesh.tolist()}"
                )

            _backend = "gloo" if self.device_type == "cpu" else "nccl"
            init_process_group(backend=_backend)
        return _get_default_group()

    def __enter__(self) -> "DeviceMesh":
        # set global device_mesh to this instance
        set_global_device_mesh(self)
        return self

    # pyre-fixme[2]: Parameter must be annotated.
    def __exit__(self, exc_type, exc_value, exc_traceback) -> None:
        # unset global device mesh
        set_global_device_mesh(None)

    def __repr__(self) -> str:
        return f"DeviceMesh:({self.mesh.tolist()})"

    def __hash__(self):
        return hash((self.mesh, id(self)))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DeviceMesh):
            return False
        if id(self) == id(other):
            return True
        return self.mesh.equal(other.mesh)

    def get_dim_groups(self) -> List[ProcessGroup]:
        return self._dim_groups

    # pyre-fixme[3]: Return type must be annotated.
    def size(self, dim: int = 0):
        return self.mesh.size(dim)

    @property
    def ndim(self) -> int:
        return self.mesh.ndim

    def backend(self) -> str:
        return self._backend

    def get_rank(self) -> int:
        return get_rank()

    def get_coordinate(self) -> Optional[List[int]]:
        """
        Return the relative indices of this rank relative to all
        dimensions of the mesh. If this rank is not part of the mesh, return None.
        """
        return self._coordinate_on_dim if self._coordinate_on_dim else None

    def scatter(
        self,
        output: torch.Tensor,
        scatter_list: List[torch.Tensor],
        mesh_dim: int = 0,
        async_op: bool = False,
    ) -> Optional[Work]:
        """
        scatter a list of tensors to a device mesh dimension. We by default
        use the first rank of the mesh dimension as the source of truth, i.e
        for a 2d mesh [[0, 1], [2, 3]], if we scatter on mesh_dim = 1, we will
        scatter the tensor list on rank 0 to rank 0/1, and tensor list on rank
        2 to rank 2/3.

        Args:
            output (torch.Tensor): the tensor to receive the scattered list.
            scatter_list (List[torch.Tensor]): the tensor list to be scattered.
            mesh_dim (int, optional): indicate which mesh dimension we want
                to scatter on, we by default choose the first rank on the
                mesh dimension as source of truth.

        Returns:
            A :class:`Work` object
        """
        # TODO: Ideally we should use the meta tensor way
        # (to register a meta kernel for the collective op)
        # so that it would avoid the communication. Need to
        # remove the check below once that is done.
        if output.is_meta:
            return None
        dim_group = self._dim_groups[mesh_dim]
        # src need to be global rank
        src_for_dim = 0
        if dim_group is not GroupMember.WORLD:
            src_for_dim = get_global_rank(dim_group, 0)

        if src_for_dim == get_rank():
            fut = scatter(
                output,
                scatter_list=scatter_list,
                src=src_for_dim,
                group=dim_group,
                async_op=async_op,
            )
        else:
            fut = scatter(
                output,
                scatter_list=None,
                src=src_for_dim,
                group=dim_group,
                async_op=async_op,
            )

        return fut

    def broadcast(
        self,
        tensor: torch.Tensor,
        mesh_dim: int = 0,
        async_op: bool = False,
    ) -> Optional[Work]:
        """
        broadcast the tensor to a device mesh dimension. We by default
        use the first rank of the mesh dimension as the source of truth, i.e
        for a 2d mesh [[0, 1], [2, 3]], if we broadcast on mesh_dim = 1, we will
        broadcast the tensor on rank 0 to rank 0/1, and tensor on rank 2
        to rank 2/3.

        Args:
            tensor (torch.Tensor): tensor to broadcast.
            mesh_dim (int, optional): indicate which mesh dimension we want
                to scatter on, we by default choose the first rank on the
                mesh dimension as source of truth.

        Returns:
            A :class:`Work` object
        """
        # TODO: Ideally we should use the meta tensor way
        # (to register a meta kernel for the collective op)
        # so that it would avoid the communication. Need to
        # remove the check below once that is done.
        if tensor.is_meta:
            return None
        dim_group = self._dim_groups[mesh_dim]
        # src need to be global rank
        src_for_dim = 0
        if dim_group is not GroupMember.WORLD:
            src_for_dim = get_global_rank(dim_group, 0)

        return broadcast(tensor, src=src_for_dim, group=dim_group, async_op=async_op)

    def all_gather(
        self,
        tensor: torch.Tensor,
        mesh_dim: int = 0,
        gather_dim: int = 0,
    ) -> torch.Tensor:
        """
        all_gather the tensor on each rank to the tensor_list on a
        device mesh dimension.

        Args:
            tensor_list (List[torch.Tensor]): The gathered tensor list.
            tensor (torch.Tensor): tensor to be gathered on each rank.
            mesh_dim (int, optional): indicate which mesh dimension we want
                to scatter on, we by default choose the first rank on the
                mesh dimension as source of truth.
            gather_dim (int, optional): Dimension to concatenate the resulting tensor.
            gather_size (torch.Size, optional): Desired shape of the gathered tensor.

            This suppport up to 1 elem of padding on gather_dim.
        Returns:
            A :class:`torch.Tensor` object
        """
        dim_group = self._dim_groups[mesh_dim]
        return funcol.all_gather_tensor(tensor, gather_dim=gather_dim, group=dim_group)

    def all_reduce(
        self,
        tensor: torch.Tensor,
        op: ReduceOp = ReduceOp.SUM,  # type: ignore[assignment]
        mesh_dim: int = 0,
        async_op: bool = False,
    ) -> torch.Tensor:
        """
        all_reduce the tensor on each rank on a device mesh dimension, and
        return an output tensor on each rank after all_reduce.

        Args:
            tensor (torch.Tensor): tensor to be all_reduced on each rank.
            op (:class:`torch.distributed.distributed_c10d.ReduceOp, optional):
                the reduction op of all_reduce (i.e. ReduceOp.SUM)
            mesh_dim (int, optional): indicate which mesh dimension we want
                to reduce on.

        Returns:
            A :class:`torch.Tensor` object
        """
        dim_group = self._dim_groups[mesh_dim]
        op_name: str = op.name  # type: ignore[attr-defined]
        return funcol.all_reduce(tensor, reduceOp=op_name, group=(self, mesh_dim,))

    def reduce_scatter(
        self,
        input: torch.Tensor,
        op: ReduceOp = ReduceOp.SUM,  # type: ignore[assignment]
        mesh_dim: int = 0,
        scatter_dim: int = 0,
    ) -> torch.Tensor:
        """
        reduce the input on each rank on a device mesh dimension, and scatter
        the results to the output tensor on each rank.

        Args:
            input (torch.Tensor): tensor to be reduced and scattered
                and scattered on each rank.
            op (:class:`torch.distributed.distributed_c10d.ReduceOp, optional):
                the reduction op of reduce_scatter (i.e. ReduceOp.SUM)
            mesh_dim (int, optional): indicate which mesh dimension we want
                to scatter on.

        Returns:
            A :class:`torch.Tensor` object
        """
        op_name: str = op.name  # type: ignore[attr-defined]
        # my_coordinate = self.get_coordinate()
        # TODO: what should happen if rank is not in the mesh?
        # see issue https://github.com/pytorch/tau/pull/492
        # assert (
        #     my_coordinate is not None
        # ), "Rank if not part of mesh"  # TODO: figure out behavior here

        # num_chunks = self.size(dim=mesh_dim)
        # needs_padding = False
        # if input.size(0) % num_chunks != 0:
        #     from torch.distributed._tensor.placement_types import Shard
        #     shard = Shard(scatter_dim)
        #     scattered_list, pad_idx = shard._split_tensor(
        #         input, num_chunks, with_padding=True, contiguous=True
        #     )
        #     input = torch.cat(scattered_list)
        #     needs_padding = True

        if self._backend == "nccl" or self._backend == "threaded":
            dim_group = self._dim_groups[mesh_dim]
            scatter_tensor = funcol.reduce_scatter_tensor(input, reduceOp=op_name, scatter_dim=scatter_dim, group=dim_group)

        elif self._backend == "gloo":
            # it's gloo, which does not have reduce_scatter
            # we have to do all_reduce + scatter
            warnings.warn(
                "ProcessGroupGloo does not support reduce_scatter, falling back with all reduce!"
            )
            dim_group = self._dim_groups[mesh_dim]
            flat_tensor = funcol.all_reduce(input, reduceOp=op_name, group=dim_group)
            chunks = flat_tensor.chunk(get_world_size(dim_group))
            scatter_tensor = chunks[get_rank(dim_group)]
        else:
            raise RuntimeError(
                f"backend {self._backend} does not support reduce_scatter!"
            )

        # if needs_padding:
        #     if pad_idx != 0 and my_coordinate[mesh_dim] >= pad_idx:
        #         scatter_tensor = shard._unpad_tensor(scatter_tensor)

        return scatter_tensor

    # TODO: test uneven split on GLOO and NCCL
    def all_to_all(
        self,
        output_tensor_list: List[torch.Tensor],
        input_tensor_list: List[torch.Tensor],
        mesh_dim: int = 0,
        async_op: bool = False,
    ) -> Optional[Work]:
        dim_group = self._dim_groups[mesh_dim]

        work = None
        # no direct dist.all_to_all support on 'gloo' so we manually do scatters
        if self.backend() == "gloo":
            # TODO: pull the handle of uneven case in #492
            dim_group_size = get_world_size(dim_group)
            for i in range(dim_group_size):
                # src need to be global rank
                src_for_dim = i
                if dim_group is not GroupMember.WORLD:
                    src_for_dim = get_global_rank(dim_group, i)

                work = scatter(
                    output_tensor_list[i],
                    input_tensor_list if self.get_rank() == src_for_dim else [],
                    group=dim_group,
                    src=src_for_dim,
                    async_op=async_op,
                )

        elif self.backend() == "nccl":
            work = all_to_all(
                output_tensor_list,
                input_tensor_list,
                dim_group,
                async_op=async_op,
            )
        else:
            raise RuntimeError(
                f"DeviceMesh does not support all-to-all collective operations on {self.backend()} backend."
            )
        return work
