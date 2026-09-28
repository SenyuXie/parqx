"""Shared operations for bounded Arrow batches."""

import pyarrow as pa

from parqx.data.view import DataPage

DEFAULT_PAGE_ROWS = 256
DEFAULT_PAGE_BYTES = 4 * 1024 * 1024


def bounded_prefix_rows(
    batch: pa.RecordBatch, max_bytes: int, *, allow_one: bool
) -> int:
    """Find a prefix within a byte budget, optionally admitting one oversized row."""
    low, high = 0, batch.num_rows
    while low < high:
        middle = (low + high + 1) // 2
        if batch.slice(0, middle).nbytes <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return max(low, 1 if allow_one and batch.num_rows else 0)


class PageBuilder:
    """Collect a contiguous window from ordered batches without retaining their buffers.

    Sources supply batches with absolute row offsets and handle their own I/O,
    cancellation, and locking. A single oversized row is admitted for progress.
    """

    def __init__(
        self,
        schema: pa.Schema,
        start: int,
        stop: int,
        *,
        row_count: int,
        max_rows: int,
        max_bytes: int,
    ) -> None:
        """Clamp a requested window to the available rows and page budget."""
        self.start = max(0, min(start, row_count))
        self.stop = min(max(self.start, stop), self.start + max_rows, row_count)
        self._schema = schema
        self._max_bytes = max_bytes
        self._batches: list[pa.RecordBatch] = []
        self._rows = 0
        self._used_bytes = 0

    def append(self, batch: pa.RecordBatch, offset: int) -> bool:
        """Append the intersecting prefix; return True when the page is full."""
        if self._rows >= self.stop - self.start:
            return True
        skip = max(0, self.start - offset)
        length = min(batch.num_rows - skip, self.stop - self.start - self._rows)
        if length <= 0:
            return False
        batch = batch.slice(skip, length)
        keep = bounded_prefix_rows(
            batch, self._max_bytes - self._used_bytes, allow_one=self._rows == 0
        )
        if keep:
            # Arrow slices share buffers; take releases the rest of the decode batch.
            batch = batch.take(pa.array(range(keep), type=pa.int64()))
            self._batches.append(batch)
            self._rows += keep
            self._used_bytes += batch.nbytes
        return (
            keep < length
            or self._rows >= self.stop - self.start
            or self._used_bytes >= self._max_bytes
        )

    def to_page(self) -> DataPage:
        """Assemble the collected batches, preserving schema for empty windows."""
        return DataPage(self.start, pa.Table.from_batches(self._batches, self._schema))
