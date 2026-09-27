"""Metadata-first, bounded Parquet window reads."""

from __future__ import annotations

from bisect import bisect_right
from pathlib import Path
from threading import Event

import pyarrow as pa
import pyarrow.parquet as pq

from parqx.data.batch import bounded_prefix
from parqx.data.view import DataPage


class ReadCancelledError(Exception):
    """A newer data window superseded this read."""


class ParquetSource:
    """Read footer metadata once and reopen the file for cancellable windows."""

    def __init__(
        self, path: Path, *, page_rows: int = 256, page_bytes: int = 4 * 1024 * 1024
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
        start = max(0, min(start, self.row_count))
        stop = min(max(start, stop), start + self.page_rows, self.row_count)
        batches: list[pa.RecordBatch] = []
        rows = size = 0
        with pq.ParquetFile(self.path) as file:
            group = max(0, bisect_right(self._offsets, start) - 1)
            for index in range(group, len(self._offsets) - 1):
                offset = self._offsets[index]
                if offset >= stop:
                    break
                for batch in file.iter_batches(
                    batch_size=self.page_rows, row_groups=[index]
                ):
                    if cancelled.is_set():
                        raise ReadCancelledError
                    end = offset + batch.num_rows
                    if end <= start:
                        offset = end
                        continue
                    skip = max(0, start - offset)
                    length = min(batch.num_rows - skip, stop - start - rows)
                    batch = batch.slice(skip, length)
                    keep = bounded_prefix(
                        batch, self.page_bytes - size, allow_one=rows == 0
                    )
                    if keep:
                        # Compact a slice to release the rest of the decode batch.
                        batch = batch.take(pa.array(range(keep), type=pa.int64()))
                        batches.append(batch)
                        rows += keep
                        size += batch.nbytes
                    if keep < length or rows >= stop - start or size >= self.page_bytes:
                        return DataPage(
                            start, pa.Table.from_batches(batches, self.schema)
                        )
                    offset = end
        return DataPage(start, pa.Table.from_batches(batches, self.schema))
