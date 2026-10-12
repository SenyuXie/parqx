"""Worker-owned DuckDB connections and bounded Arrow results."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from tempfile import TemporaryDirectory
from typing import cast

import duckdb
import pyarrow as pa


@dataclass(frozen=True)
class QueryPreview:
    """A bounded result; truncation does not limit the query's input."""

    table: pa.Table
    """Result rows retained within the preview budgets."""
    reason: str | None = None
    """Budget that stopped reading, or None for a complete result."""

    @property
    def truncated(self) -> bool:
        """Whether a preview budget stopped reading the result."""
        return self.reason is not None


@contextmanager
def connect(
    *, memory_limit: str, threads: int
) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """Own one connection and spill directory on the calling worker thread.

    Close Arrow readers inside this context before releasing the connection.
    """
    with ExitStack() as resources:
        temporary = TemporaryDirectory(prefix="parqx-duckdb-")
        resources.callback(temporary.cleanup)
        connection = duckdb.connect(
            config={
                "threads": threads,
                "memory_limit": memory_limit,
                "temp_directory": temporary.name,
                "preserve_insertion_order": True,
                "python_enable_replacements": False,
            }
        )
        resources.callback(connection.close)
        yield connection


def read_preview(
    reader: pa.RecordBatchReader, *, row_limit: int, byte_limit: int
) -> QueryPreview:
    """Read bounded Arrow rows, leaving reader cleanup to the caller.

    One oversized row is admitted. Decoder and engine allocations are separate
    from the retained preview budget.
    """
    schema = reader.schema
    batches: list[pa.RecordBatch] = []
    rows = size = 0
    reason: str | None = None
    while True:
        try:
            batch = reader.read_next_batch()
        except StopIteration:
            break
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
    return QueryPreview(pa.Table.from_batches(batches, schema=schema), reason=reason)


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


def _dictionary_payload_exceeds_budget(
    array: pa.DictionaryArray, indices: list[int | None], budget: int
) -> bool:
    data_type = array.dictionary.type
    if not (
        pa.types.is_string(data_type)
        or pa.types.is_large_string(data_type)
        or pa.types.is_binary(data_type)
        or pa.types.is_large_binary(data_type)
        or pa.types.is_fixed_size_binary(data_type)
    ):
        return False
    used: set[int] = set()
    size = 0
    for index in indices:
        if index is not None and index not in used and array.dictionary[index].is_valid:
            used.add(index)
            # A singleton may include one validity byte. Excluding it gives a
            # lower bound without copying large strings just to reject a prefix.
            size += max(0, array.dictionary.slice(index, 1).nbytes - 1)
            if size > budget:
                return True
    return False


def _compact_array(
    array: pa.Array, selection: list[int] | None = None, *, budget: int | None = None
) -> pa.Array:
    if selection is None:
        selection = list(range(len(array)))
    if isinstance(array, pa.DictionaryArray):
        indices = [
            cast(int | None, array.indices[index].as_py()) for index in selection
        ]
        if budget is not None and _dictionary_payload_exceeds_budget(
            array, indices, budget
        ):
            raise _BudgetExceededError
        # Keep category order even for an ordered dictionary. Gathering only used
        # codes avoids allocations proportional to the original dictionary.
        used = sorted({index for index in indices if index is not None})
        positions = {index: position for position, index in enumerate(used)}
        values = _compact_array(array.dictionary, used, budget=budget)
        return pa.DictionaryArray.from_arrays(
            pa.array(
                [positions[index] if index is not None else None for index in indices],
                type=array.indices.type,
            ),
            values,
            ordered=array.type.ordered,
        )
    try:
        array = array.take(pa.array(selection, type=pa.int64()))
    except pa.ArrowNotImplementedError:
        # View types lack take kernels. Arrow Scalars preserve nested types and
        # nanosecond values when rebuilding, unlike conversion to Python values.
        array = pa.array([array[index] for index in selection], type=array.type)
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
