"""Parquet-backed table source."""

from __future__ import annotations

from bisect import bisect_right
from itertools import accumulate
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from parqx.data.cache import BoundedLRUCache
from parqx.data.source.base import ColumnInfo

DEFAULT_MAX_CACHE_BYTES = 256 * 1024 * 1024  # 256MiB


class ParquetSource:
    """Table source backed by a Parquet file."""

    def __init__(
        self, path: Path, max_cache_bytes: int = DEFAULT_MAX_CACHE_BYTES
    ) -> None:
        """Initialize the source from a Parquet file.

        Args:
            path: Parquet file to inspect.
            max_cache_bytes: Maximum decoded row-group cache size in bytes.
        """
        self._path = path
        """Parquet file path backing this source."""
        self._pf = pq.ParquetFile(path, memory_map=True, pre_buffer=False)
        """PyArrow ParquetFile handle used for metadata and row-group reads."""
        self._schema = self._pf.schema_arrow
        """Arrow schema read from the Parquet file metadata."""

        metadata = self._pf.metadata
        row_group_sizes: list[int] = [
            metadata.row_group(index).num_rows
            for index in range(metadata.num_row_groups)
        ]
        self._row_group_starts = list(accumulate(row_group_sizes, initial=0))
        """Prefix sum of row-group start offsets; final item is total row count."""

        self._columns = tuple(
            ColumnInfo(field.name, field.type) for field in self._schema
        )
        """Column metadata in source order, derived once from the Arrow schema."""

        self._cache: BoundedLRUCache[int, pa.Table] = BoundedLRUCache(
            max_bytes=max_cache_bytes, sizeof=lambda table: table.nbytes
        )
        """Decoded row-group cache keyed by row-group index."""
        self._closed = False
        """Whether this source has been closed."""

    @property
    def row_count(self) -> int:
        """Total number of rows exposed by this source."""
        return self._row_group_starts[-1]

    @property
    def column_count(self) -> int:
        """Total number of columns exposed by this source."""
        return len(self._columns)

    @property
    def columns(self) -> tuple[ColumnInfo, ...]:
        """Column metadata in source order. Stable across calls."""
        return self._columns

    def _locate_row_group(self, row: int) -> tuple[int, int]:
        """Return (row_group_index, local_row_index) for a global row index."""
        row_group_index = bisect_right(self._row_group_starts, row) - 1
        local_row_index = row - self._row_group_starts[row_group_index]
        return row_group_index, local_row_index

    def _load_row_group(self, row_group_index: int) -> pa.Table:
        """Return the decoded table for a row group, loading it on cache miss."""
        if row_group_index in self._cache:
            return self._cache[row_group_index]

        table = self._pf.read_row_group(row_group_index, use_threads=True)
        self._cache[row_group_index] = table
        return table

    def get_cell_at(self, row: int, column: int) -> pa.Scalar:
        """Return the value at (row, column).

        Args:
            row: Zero-based row index of the value to retrieve.
            column: Zero-based column index of the value to retrieve.

        Raises:
            IndexError: If row or column is out of range.
        """
        if not (0 <= row < self.row_count):
            raise IndexError(row)
        if not (0 <= column < self.column_count):
            raise IndexError(column)

        row_group_index, local_row_index = self._locate_row_group(row)
        table = self._load_row_group(row_group_index)
        return table.column(column)[local_row_index]

    def close(self) -> None:
        """Release file handles and cached row groups."""
        if self._closed:
            return

        self._closed = True
        self._cache.clear()
        self._pf.close()
