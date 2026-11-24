"""Cache-only table access for synchronous rendering."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import cast

import pyarrow as pa


def row_value(table: pa.Table, row: int) -> pa.StructScalar:
    """Read one row as a Struct, preserving field order, types and duplicate names.

    Args:
        table: Loaded Arrow data for the result or source page.
        row: Zero-based offset within this table.
    """
    return cast(pa.StructScalar, table.slice(row, 1).to_struct_array()[0])


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


class PageCache:
    """UI-owned metadata and a bounded Arrow page cache, without I/O on lookup.

    A missing cell returns None, distinct from an Arrow null scalar. Lazy views
    retain only source metadata until bounded pages arrive.
    """

    def __init__(self, schema: pa.Schema, row_count: int, *, cache_bytes: int) -> None:
        """Create a metadata-only view with an empty page cache."""
        if cache_bytes < 1 or row_count < 0:
            raise ValueError("Cache budget must be positive and row count nonnegative")
        self.schema = schema
        self.row_count = row_count
        self.cache_budget = cache_bytes
        self.cache_bytes = 0
        self._pages: OrderedDict[int, DataPage] = OrderedDict()

    def peek(self, row: int, column: int | None) -> pa.Scalar | None:
        """Return a resident cell or row without performing I/O.

        Args:
            row: Absolute row in the source.
            column: Positional column index, or None for the entire row.

        Returns:
            An Arrow scalar (possibly null), or None for a missing page.
        """
        if not (
            0 <= row < self.row_count
            and (column is None or 0 <= column < len(self.schema))
        ):
            raise IndexError((row, column))
        for key in reversed(self._pages):
            page = self._pages[key]
            if page.start <= row < page.stop:
                self._pages.move_to_end(key)
                offset = row - page.start
                if column is None:
                    return row_value(page.table, offset)
                return page.table.column(column)[offset]
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
