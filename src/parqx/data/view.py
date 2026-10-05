"""Cache-only table access for synchronous rendering."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

import pyarrow as pa


@dataclass(frozen=True)
class DataPage:
    """An immutable Arrow window at an absolute row offset."""

    start: int
    """Zero-based row offset of this window in the full result."""
    table: pa.Table
    """Arrow data for the contiguous rows beginning at start."""

    @property
    def stop(self) -> int:
        """Exclusive end of this window."""
        return self.start + self.table.num_rows


class TableData:
    """UI-owned metadata and a bounded Arrow page cache, without I/O on lookup.

    A missing cell returns None, distinct from an Arrow null scalar. Lazy views
    keep an empty width sample; the widget measures pages as they arrive.
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

    def add_page(self, page: DataPage) -> None:
        """Cache a completed window.

        Keep at most 128 pages within the logical byte budget. A single oversized
        page is admitted so a cell larger than the budget can still be inspected.
        """
        if not page.table.num_rows:
            return
        if old := self._pages.pop(page.start, None):
            self.cache_bytes -= old.table.nbytes
        self._pages[page.start] = page
        self.cache_bytes += page.table.nbytes
        while len(self._pages) > 1 and (
            self.cache_bytes > self.cache_budget or len(self._pages) > 128
        ):
            _, removed = self._pages.popitem(last=False)
            self.cache_bytes -= removed.table.nbytes
