from __future__ import annotations

from collections.abc import Iterable
from typing import Any, overload

from . import Array, ChunkedArray, DataType, MemoryPool, Scalar, StructScalar

type ArrayLike = Array | ChunkedArray | Iterable[Any]

class ScalarAggregateOptions:
    def __init__(self, *, skip_nulls: bool = True, min_count: int = 1) -> None: ...

class CastOptions:
    def __init__(
        self,
        target_type: DataType | None = None,
        *,
        allow_int_overflow: bool | None = None,
        allow_time_truncate: bool | None = None,
        allow_time_overflow: bool | None = None,
        allow_decimal_truncate: bool | None = None,
        allow_float_truncate: bool | None = None,
        allow_invalid_utf8: bool | None = None,
    ) -> None: ...
    @staticmethod
    def safe(target_type: DataType | None = None) -> CastOptions: ...
    @staticmethod
    def unsafe(target_type: DataType | None = None) -> CastOptions: ...
    def is_safe(self) -> bool: ...

@overload
def cast(
    arr: Scalar,
    target_type: DataType | str,
    safe: bool | None = None,
    options: CastOptions | None = None,
    memory_pool: MemoryPool | None = None,
) -> Scalar: ...
@overload
def cast(
    arr: Array,
    target_type: DataType | str,
    safe: bool | None = None,
    options: CastOptions | None = None,
    memory_pool: MemoryPool | None = None,
) -> Array: ...
@overload
def cast(
    arr: ChunkedArray,
    target_type: DataType | str,
    safe: bool | None = None,
    options: CastOptions | None = None,
    memory_pool: MemoryPool | None = None,
) -> ChunkedArray: ...
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
