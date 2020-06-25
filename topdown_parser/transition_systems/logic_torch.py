from typing import Tuple, Set

import torch


def are_eq(a : torch.Tensor, b : torch.Tensor) -> torch.BoolTensor:
    if a.dtype == torch.float or a.dtype == torch.float16 or b.dtype == torch.float or b.dtype == torch.float16:
        return torch.abs(a-b) < 0.00001
    else:
        return a == b


def tensor_or(tensor : torch.BoolTensor, dim : int) -> torch.BoolTensor:
    return tensor.sum(dim=dim) > 0


def index_OR(batched_set : torch.BoolTensor, mapping : torch.BoolTensor) -> torch.BoolTensor:
    """
    batched_set : shape (batch_size, set capacity)
    mapping : shape (set capacity, "constants")
    returns a bool tensor R of shape (batch_size, "constants")
    R[b,c] = True iff \exists l, batched_set[b,l] AND mapping[l,c]
    """
    result = batched_set @ mapping #shape (batch_size, "constants")
    return result > 0

def batched_index_OR(batched_set : torch.BoolTensor, mapping : torch.BoolTensor) -> torch.BoolTensor:
    """
    batched_set : shape (batch_size, set capacity)
    mapping : shape (batch_size, set capacity, "constants")
    returns a bool tensor R of shape (batch_size, "constants")
    R[b,c] = True iff \exists l, batched_set[b,l] AND mapping[b,l,c]
    """
    result = (torch.bmm(batched_set.unsqueeze(1), mapping)).squeeze(1) #shape (batch_size, "lexical types")
    return result > 0


def consistent_with_and_can_finish_now(batched_set: torch.BoolTensor, mapping: torch.BoolTensor, apply_set_exists : torch.BoolTensor,
                                       minimal_apply_set_size : torch.Tensor) -> Tuple[torch.BoolTensor, torch.BoolTensor]:
    """
    batched_set : shape (batch_size, set capacity)
    mapping : shape (batch_size, set capacity, "lexical types")
    apply_set_exists : shape (batch_size, "lexical types")
    minimal_apply_set_size: shape (batch_size, "lexical types")

    returns a tuple of bool tensors, both of shape (batch_size, "lexical types")
    1.) R[b,l] = True iff \forall s, batchet_set[b,s] <-> mapping[b, s, l]
    2.) R[b,l] = True iff \forall s, batchet_set[b,s] -> mapping[b, s, l] AND apply_set_exists[b,l]
    """
    set_size = batched_set.sum(dim=1) #shape (batch_size,)
    #result = torch.einsum("bs, bsl -> bl", batched_set, mapping)
    result = (torch.bmm(batched_set.unsqueeze(1), mapping)).squeeze(1) #shape (batch_size, "lexical types")
    #s = mapping.sum(dim=1) #shape (batch_size, "lexical types",), contains the size of the apply set for each lexical type

    consistent = are_eq(result, set_size.unsqueeze(1)) #shape (batch_size, "lexical types")
    # OK but does not take into account that not every pair of types is apply-reachable, so find lexical types that are apply reachable to our
    # set of term types
    consistent &= apply_set_exists

    can_finish_now = consistent & (result >= minimal_apply_set_size)
    #can_finish_now = consistent & are_eq(result, s)

    return can_finish_now, consistent

def debug_to_set(t : torch.BoolTensor) -> Set[int]:
    return [{ i for i,x in enumerate(batch) if x} for batch in t.numpy()]