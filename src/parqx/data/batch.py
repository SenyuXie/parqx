"""Shared operations for bounded Arrow batches."""

import pyarrow as pa


def bounded_prefix(batch: pa.RecordBatch, budget: int, *, allow_one: bool) -> int:
    """Find a prefix within a byte budget, optionally admitting one oversized row."""
    low, high = 0, batch.num_rows
    while low < high:
        middle = (low + high + 1) // 2
        if batch.slice(0, middle).nbytes <= budget:
            low = middle
        else:
            high = middle - 1
    return max(low, 1 if allow_one and batch.num_rows else 0)


def compact_batch(batch: pa.RecordBatch) -> pa.RecordBatch:
    """Copy a selected batch without retaining discarded backing buffers."""
    try:
        return batch.take(pa.array(range(batch.num_rows), type=pa.int64()))
    except pa.ArrowNotImplementedError:
        # View types lack take kernels. Arrow Scalars preserve nested types and
        # nanosecond values when rebuilding, unlike conversion to Python values.
        return pa.RecordBatch.from_arrays(
            [
                pa.array(list(batch.column(index)), type=field.type)
                for index, field in enumerate(batch.schema)
            ],
            schema=batch.schema,
        )
