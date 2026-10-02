"""Shared operations for bounded Arrow batches."""

from typing import cast

import pyarrow as pa


class _BudgetExceededError(Exception):
    """Stop measuring before copying oversized dictionary values."""


def _has_dictionary(data_type: pa.DataType) -> bool:
    return isinstance(data_type, pa.DictionaryType) or any(
        _has_dictionary(data_type.field(index).type)
        for index in range(data_type.num_fields)
    )


def bounded_prefix(batch: pa.RecordBatch, budget: int, *, allow_one: bool) -> int:
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


def compact_batch(batch: pa.RecordBatch, *, copy: bool = True) -> pa.RecordBatch:
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
