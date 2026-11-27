"""Worker-owned DuckDB connections and bounded Arrow results."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from tempfile import TemporaryDirectory
from typing import Literal

import duckdb
import pyarrow as pa
import pyarrow.compute as pc

type TruncationReason = Literal["row limit", "byte budget"]


@contextmanager
def connect(
    *, memory_limit: str, threads: int
) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """Own one connection and spill directory on the calling worker thread.

    Close Arrow readers inside this context before releasing the connection.
    """
    with (
        TemporaryDirectory(prefix="parqx-duckdb-") as directory,
        duckdb.connect(
            config={
                "threads": threads,
                "memory_limit": memory_limit,
                "temp_directory": directory,
                "preserve_insertion_order": True,
                "python_enable_replacements": False,
            }
        ) as connection,
    ):
        yield connection


def read_preview(
    reader: pa.RecordBatchReader, *, row_limit: int, byte_limit: int
) -> tuple[pa.Table, TruncationReason | None]:
    """Read bounded Arrow rows, leaving reader cleanup to the caller.

    One oversized row is admitted. Decoder and engine allocations are separate
    from the retained preview budget.

    Args:
        reader: Open Arrow batch stream, owned by the caller.
        row_limit: Maximum number of rows in the preview.
        byte_limit: Maximum logical size of retained Arrow batches in bytes.

    Returns:
        The preview table and the reason for truncation, if any.
    """
    schema = reader.schema
    batches: list[pa.RecordBatch] = []
    rows = size = 0
    reason: TruncationReason | None = None
    for batch in reader:
        if not batch.num_rows:
            continue
        available = min(batch.num_rows, row_limit - rows)
        if available <= 0 or size >= byte_limit:
            reason = "row limit" if available <= 0 else "byte budget"
            break
        keep = _bounded_prefix(
            batch.slice(0, available), byte_limit - size, allow_one=rows == 0
        )
        if keep:
            # A slice could retain all the discarded rows' backing buffers.
            prefix = _compact_batch(batch.slice(0, keep), copy=keep < batch.num_rows)
            batches.append(prefix)
            rows += keep
            size += prefix.nbytes
        if keep < batch.num_rows:
            reason = "row limit" if rows >= row_limit else "byte budget"
            break
    return pa.Table.from_batches(batches, schema=schema), reason


class _BudgetExceededError(Exception):
    """Stop measuring before copying oversized dictionary values."""


def _has_dictionary(data_type: pa.DataType) -> bool:
    return isinstance(data_type, pa.DictionaryType) or any(
        _has_dictionary(data_type.field(index).type)
        for index in range(data_type.num_fields)
    )


def _bounded_prefix(batch: pa.RecordBatch, budget: int, *, allow_one: bool) -> int:
    """Find a prefix within a byte budget, optionally admitting one oversized row."""
    dictionary_columns = [
        index for index, field in enumerate(batch.schema) if _has_dictionary(field.type)
    ]
    low, high = 0, batch.num_rows
    while low < high:
        middle = (low + high + 1) // 2
        prefix = batch.slice(0, middle)
        size = prefix.nbytes
        # Slices count the entire dictionary, including unused categories. Measure
        # only referenced values, just as the retained compact batch will do.
        for index in dictionary_columns:
            column = prefix.column(index)
            try:
                size += _compact_array(column, budget=budget).nbytes - column.nbytes
            except _BudgetExceededError:
                size = budget + 1
                break
        if size <= budget:
            low = middle
        else:
            high = middle - 1
    return max(low, 1 if allow_one and batch.num_rows else 0)


def _compact_dictionary_values(
    dictionary: pa.Array, used: pa.Array, budget: int | None
) -> pa.Array:
    """Gather referenced categories in original order within the preview budget."""
    data_type = dictionary.type
    if not (
        pa.types.is_string(data_type)
        or pa.types.is_large_string(data_type)
        or pa.types.is_binary(data_type)
        or pa.types.is_large_binary(data_type)
        or pa.types.is_fixed_size_binary(data_type)
    ):
        return _compact_array(dictionary, used, budget=budget)
    if not len(used):
        return pa.array([], type=data_type)

    # take may reserve space based on large unselected strings. Gather
    # zero-copy slices and check their size before copying the payload.
    first = used[0].as_py()
    span = dictionary.slice(first, used[-1].as_py() - first + 1)
    if len(span) == len(used) and not span.null_count:
        if budget is not None and span.nbytes - 1 > budget:
            raise _BudgetExceededError
        return pa.concat_arrays([span])

    parts: list[pa.Array] = []
    for code in used:
        entry = dictionary.slice(code.as_py(), 1)
        if entry.null_count and not pa.types.is_fixed_size_binary(data_type):
            # A null string may still have a nonempty underlying byte range.
            entry = pa.array([None], type=data_type)
        if budget is not None:
            # Singleton slices may count the same validity byte.
            budget -= max(0, entry.nbytes - 1)
            if budget < 0:
                raise _BudgetExceededError
        parts.append(entry)
    return pa.concat_arrays(parts)


def _compact_array(
    array: pa.Array, selection: pa.Array | None = None, *, budget: int | None = None
) -> pa.Array:
    if isinstance(array, pa.DictionaryArray):
        indices = array.indices
        if selection is not None:
            indices = indices.take(selection)
        # Retain the relative category order of ordered dictionaries.
        used = indices.unique().drop_null().sort()
        values = _compact_dictionary_values(array.dictionary, used, budget)
        positions = pc.index_in(indices, value_set=used)
        return pa.DictionaryArray.from_arrays(
            pc.cast(positions, indices.type), values, ordered=array.type.ordered
        )
    if selection is None:
        selection = pa.array(range(len(array)), type=pa.int64())
    try:
        array = array.take(selection)
    except pa.ArrowNotImplementedError:
        # View types lack take kernels. Arrow Scalars preserve nested types and
        # nanosecond values when rebuilding, unlike conversion to Python values.
        array = pa.array([array[i.as_py()] for i in selection], type=array.type)
    if not _has_dictionary(array.type):
        return array
    if isinstance(array, pa.StructArray):
        children = [
            _compact_array(array.field(index), budget=budget)
            for index in range(array.type.num_fields)
        ]
    elif isinstance(
        array, (pa.ListArray, pa.LargeListArray, pa.FixedSizeListArray, pa.MapArray)
    ):
        children = [_compact_array(array.values, budget=budget)]
    else:
        return array
    # take normalizes parent offsets; reuse its small structural buffers while
    # replacing nested children whose dictionaries still retain unused values.
    return pa.Array.from_buffers(
        array.type,
        len(array),
        array.buffers()[: array.type.num_buffers],
        null_count=array.null_count,
        children=children,
    )


def _compact_batch(batch: pa.RecordBatch, *, copy: bool = True) -> pa.RecordBatch:
    """Copy selected rows and dictionary values without discarded backing buffers."""
    return pa.RecordBatch.from_arrays(
        [
            _compact_array(batch.column(index))
            if copy or _has_dictionary(field.type)
            else batch.column(index)
            for index, field in enumerate(batch.schema)
        ],
        schema=batch.schema,
    )
