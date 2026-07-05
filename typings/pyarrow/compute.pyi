from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from . import Array, ChunkedArray, MemoryPool, Scalar, StructScalar

type ArrayLike = Array | ChunkedArray | Iterable[Any]

class ScalarAggregateOptions:
    def __init__(self, *, skip_nulls: bool = True, min_count: int = 1) -> None: ...

def min(
    array: ArrayLike,
    /,
    *,
    skip_nulls: bool = True,
    min_count: int = 1,
    options: ScalarAggregateOptions | None = None,
    memory_pool: MemoryPool | None = None,
) -> Scalar: ...
def max(
    array: ArrayLike,
    /,
    *,
    skip_nulls: bool = True,
    min_count: int = 1,
    options: ScalarAggregateOptions | None = None,
    memory_pool: MemoryPool | None = None,
) -> Scalar: ...
def min_max(
    array: ArrayLike,
    /,
    *,
    skip_nulls: bool = True,
    min_count: int = 1,
    options: ScalarAggregateOptions | None = None,
    memory_pool: MemoryPool | None = None,
) -> StructScalar: ...
