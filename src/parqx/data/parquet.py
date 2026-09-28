"""Metadata-first, bounded Parquet window reads."""

from __future__ import annotations

from bisect import bisect_right
from pathlib import Path
from threading import Event

import pyarrow.parquet as pq

from parqx.data.batch import DEFAULT_PAGE_BYTES, DEFAULT_PAGE_ROWS, PageBuilder
from parqx.data.view import DataPage, ReadCancelledError


class ParquetSource:
    """Read footer metadata once and reopen the file for cancellable windows."""

    def __init__(
        self,
        path: Path,
        *,
        page_rows: int = DEFAULT_PAGE_ROWS,
        page_bytes: int = DEFAULT_PAGE_BYTES,
    ) -> None:
        """Read the schema and row-group index, without reading column data."""
        if min(page_rows, page_bytes) < 1:
            raise ValueError("Page budgets must be positive")
        self.path = path
        self.page_rows = page_rows
        self.page_bytes = page_bytes
        stat = path.stat()
        self._fingerprint = (stat.st_size, stat.st_mtime_ns)
        with pq.ParquetFile(path) as file:
            self.schema = file.schema_arrow
            self.row_count = file.metadata.num_rows
            self._offsets = [0]
            for group in range(file.metadata.num_row_groups):
                self._offsets.append(
                    self._offsets[-1] + file.metadata.row_group(group).num_rows
                )

    def read_window(self, start: int, stop: int, cancelled: Event) -> DataPage:
        """Locate row groups, then decode a bounded window on the worker thread."""
        stat = self.path.stat()
        if (stat.st_size, stat.st_mtime_ns) != self._fingerprint:
            raise OSError("The source file changed. Use Browse file to reopen it.")
        page = PageBuilder(
            self.schema,
            start,
            stop,
            row_count=self.row_count,
            max_rows=self.page_rows,
            max_bytes=self.page_bytes,
        )
        with pq.ParquetFile(self.path) as file:
            group = max(0, bisect_right(self._offsets, page.start) - 1)
            for index in range(group, len(self._offsets) - 1):
                offset = self._offsets[index]
                if offset >= page.stop:
                    break
                for batch in file.iter_batches(
                    batch_size=self.page_rows, row_groups=[index]
                ):
                    if cancelled.is_set():
                        raise ReadCancelledError
                    if page.append(batch, offset):
                        return page.to_page()
                    offset += batch.num_rows
        return page.to_page()
