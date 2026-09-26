"""Cache-only table access for synchronous rendering."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import Event
from typing import Protocol

import pyarrow as pa


@dataclass(frozen=True)
class DataPage:
    """An immutable Arrow window at an absolute result row offset."""

    start: int
    table: pa.Table

    @property
    def stop(self) -> int:
        """Exclusive end of this window."""
        return self.start + self.table.num_rows


class WindowSource(Protocol):
    """Backend window access; call only from a worker thread."""

    def read_window(self, start: int, stop: int, cancelled: Event) -> DataPage:
        """Read a bounded prefix of the requested window."""
        ...


class TableData:
    """UI-owned metadata and bounded Arrow cache with no I/O on lookup.

    A missing cell returns None, distinct from an Arrow null scalar. For a raw
    file row_count is known from its footer; for a streaming result it is the
    available prefix, and total_rows remains None until the reader is exhausted.
    """

    def __init__(
        self,
        schema: pa.Schema,
        row_count: int,
        total_rows: int | None,
        *,
        cache_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        """Create a metadata-only view with an empty data cache."""
        if cache_bytes < 1:
            raise ValueError("Cache budget must be positive")
        self.schema = schema
        self.row_count = row_count
        self.total_rows = total_rows
        self.cache_budget = cache_bytes
        self.cache_bytes = 0
        self.sample = pa.Table.from_batches([], schema=schema)
        self._table: pa.Table | None = None
        self._pages: OrderedDict[int, DataPage] = OrderedDict()

    @classmethod
    def from_table(cls, table: pa.Table) -> TableData:
        """Adapt an already bounded in-memory table, without copying its buffers."""
        data = cls(table.schema, table.num_rows, table.num_rows)
        data._table = data.sample = table
        return data

    def peek(self, row: int, column: int) -> pa.Scalar | None:
        """Return only cached data; never initiate a read on the rendering path."""
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
        """Cache a completed read and return whether the width sample changed.

        Keep at most one oversized page to ensure progress for an oversized row.
        Backends are responsible for splitting ordinary pages by this budget.
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
        # Compact a small sample so it cannot retain a large page's buffers.
        count = min(256, page.table.num_rows)
        while count and page.table.slice(0, count).nbytes > 256 * 1024:
            count //= 2
        if count:
            self.sample = page.table.take(pa.array(range(count), type=pa.int64()))
            return True
        return False
