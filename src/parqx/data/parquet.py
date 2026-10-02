"""Metadata-first Parquet reads with bounded retained windows."""

from __future__ import annotations

from bisect import bisect_right
from pathlib import Path
from threading import Event

import pyarrow as pa
import pyarrow.parquet as pq

from parqx.data.batch import bounded_prefix, compact_batch
from parqx.data.view import DataPage


class ReadCancelledError(Exception):
    """A newer data window superseded this read."""


def _check_cancelled(cancelled: Event) -> None:
    if cancelled.is_set():
        raise ReadCancelledError


class ParquetSource:
    """Index footer metadata and reopen the file for each background window.

    Reading starts at the containing row group's beginning. Page budgets bound
    retained rows, not Parquet decoder allocations or work to skip earlier rows.
    """

    def __init__(
        self, path: Path, *, page_rows: int = 256, page_bytes: int = 4 * 1024 * 1024
    ) -> None:
        """Read the schema and row-group index without decoding column data."""
        if min(page_rows, page_bytes) < 1:
            raise ValueError("Page budgets must be positive")
        self.path = path
        self.page_rows = page_rows
        self.page_bytes = page_bytes
        self._fingerprint = self._file_fingerprint()
        with pq.ParquetFile(path) as file:
            self.schema = file.schema_arrow
            self.row_count = file.metadata.num_rows
            self._offsets = [0]
            for group in range(file.metadata.num_row_groups):
                self._offsets.append(
                    self._offsets[-1] + file.metadata.row_group(group).num_rows
                )
        self._check_unchanged()

    def _file_fingerprint(self) -> tuple[int, int, int, int]:
        stat = self.path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def _check_unchanged(self) -> None:
        if self._file_fingerprint() != self._fingerprint:
            raise OSError("The source file changed. Reopen it to continue browsing.")

    def read_window(self, start: int, stop: int, cancelled: Event) -> DataPage:
        """Read a compact prefix of a window, checking cancellation between batches."""
        _check_cancelled(cancelled)
        start = max(0, min(start, self.row_count))
        stop = min(max(start, stop), start + self.page_rows, self.row_count)
        if start == stop:
            return DataPage(start, pa.Table.from_batches([], schema=self.schema))
        self._check_unchanged()
        batches: list[pa.RecordBatch] = []
        rows = size = 0
        done = False
        with pq.ParquetFile(self.path) as file:
            group = max(0, bisect_right(self._offsets, start) - 1)
            for index in range(group, len(self._offsets) - 1):
                offset = self._offsets[index]
                if offset >= stop:
                    break
                reader = file.iter_batches(
                    batch_size=self.page_rows, row_groups=[index]
                )
                while True:
                    _check_cancelled(cancelled)
                    try:
                        batch = next(reader)
                    except StopIteration:
                        break
                    _check_cancelled(cancelled)
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
                        # A slice alone could retain a much larger decode buffer.
                        batch = compact_batch(batch.slice(0, keep))
                        batches.append(batch)
                        rows += keep
                        size += batch.nbytes
                    if keep < length or rows >= stop - start or size >= self.page_bytes:
                        done = True
                        break
                    offset = end
                if done:
                    break
        _check_cancelled(cancelled)
        self._check_unchanged()
        return DataPage(start, pa.Table.from_batches(batches, schema=self.schema))
