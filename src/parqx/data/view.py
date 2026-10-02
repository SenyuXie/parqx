"""Cache-only table access for synchronous rendering."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

import pyarrow as pa

from parqx.data.batch import compact_batch


@dataclass(frozen=True)
class DataPage:
    """An immutable Arrow window at an absolute row offset."""

    start: int
    table: pa.Table

    @property
    def stop(self) -> int:
        """Exclusive end of this window."""
        return self.start + self.table.num_rows


class TableData:
    """UI-owned metadata and a bounded Arrow page cache, without I/O on lookup.

    A missing cell returns None, distinct from an Arrow null scalar. The separate
    width sample retains at most 256 rows and 256 KiB of logical Arrow data.
    Already loaded tables keep their existing buffers through from_table.
    """

    def __init__(
        self, schema: pa.Schema, row_count: int, *, cache_bytes: int = 32 * 1024 * 1024
    ) -> None:
        """Create a metadata-only view with an empty page cache."""
        if cache_bytes < 1 or row_count < 0:
            raise ValueError("Cache budget must be positive and row count nonnegative")
        self.schema = schema
        self.row_count = row_count
        self.cache_budget = cache_bytes
        self.cache_bytes = 0
        self.sample = pa.Table.from_batches([], schema=schema)
        self._table: pa.Table | None = None
        self._pages: OrderedDict[int, DataPage] = OrderedDict()

    @classmethod
    def from_table(cls, table: pa.Table) -> TableData:
        """Adapt an already loaded table without copying its Arrow buffers."""
        data = cls(table.schema, table.num_rows)
        data._table = data.sample = table
        return data

    def peek(self, row: int, column: int) -> pa.Scalar | None:
        """Return cached data only, preserving the difference between null and miss."""
        if not (0 <= row < self.row_count and 0 <= column < len(self.schema)):
            raise IndexError((row, column))
        if self._table is not None:
            return self._table.column(column)[row]
        for key in reversed(self._pages):
            page = self._pages[key]
            if page.start <= row < page.stop:
                self._pages.move_to_end(key)
                return page.table.column(column)[row - page.start]
        return None

    def add_page(self, page: DataPage) -> bool:
        """Cache a completed window and report whether its width sample changed.

        Keep at most 128 pages within the logical byte budget. A single oversized
        page is admitted so a cell larger than the budget can still be inspected.
        """
        if not page.table.num_rows:
            return False
        if old := self._pages.pop(page.start, None):
            self.cache_bytes -= old.table.nbytes
        self._pages[page.start] = page
        self.cache_bytes += page.table.nbytes
        while len(self._pages) > 1 and (
            self.cache_bytes > self.cache_budget or len(self._pages) > 128
        ):
            _, removed = self._pages.popitem(last=False)
            self.cache_bytes -= removed.table.nbytes
        if self.sample.num_rows:
            return False
        count = min(256, page.table.num_rows)
        while count and page.table.slice(0, count).nbytes > 256 * 1024:
            count //= 2
        if not count:
            return False
        # Copy each selected batch: Table.take could first combine the full page.
        batches = [
            compact_batch(batch)
            for batch in page.table.slice(0, count).to_batches()
            if batch.num_rows
        ]
        self.sample = pa.Table.from_batches(batches, schema=self.schema)
        return True
