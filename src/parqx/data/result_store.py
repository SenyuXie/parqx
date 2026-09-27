"""Indexed Arrow IPC batches for bounded-memory query-result browsing."""

from __future__ import annotations

from bisect import bisect_right
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock

import pyarrow as pa
import pyarrow.ipc as ipc

from parqx.data.batch import bounded_prefix
from parqx.data.parquet import ReadCancelledError
from parqx.data.view import DataPage


class ResultStore:
    """One append-only result, with serialized writes, reads and cleanup.

    The producer appends batches once. Readers locate them by output row ordinal
    instead of re-executing SQL. Only the small batch index stays resident here;
    the UI owns a separate byte-bounded cache. All disk operations run on workers.
    """

    def __init__(
        self,
        schema: pa.Schema,
        *,
        page_rows: int = 256,
        page_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        """Create a private result directory and an initially empty index."""
        self.schema = schema
        self.page_rows = page_rows
        self.page_bytes = page_bytes
        self.row_count = 0
        self.finished = False
        self._temporary = TemporaryDirectory(prefix="parqx-result-")
        self.directory = Path(self._temporary.name)
        self._starts: list[int] = []
        self._lock = Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        """Whether cleanup completed; reading this flag performs no disk I/O."""
        return self._closed

    def append(self, batch: pa.RecordBatch) -> None:
        """Publish a batch only after its complete IPC file has been written."""
        if not batch.num_rows:
            return
        with self._lock:
            if self._closed:
                raise ReadCancelledError
            path = self.directory / f"{len(self._starts)}.arrow"
            with path.open("wb") as file, ipc.new_file(file, self.schema) as writer:
                writer.write_batch(batch)
            self._starts.append(self.row_count)
            self.row_count += batch.num_rows

    def finish(self) -> None:
        """Mark the final row count known after the query reader reaches EOF."""
        self.finished = True

    def read_window(self, start: int, stop: int, cancelled: Event) -> DataPage:
        """Read only indexed batches intersecting a bounded result window."""
        batches: list[pa.RecordBatch] = []
        rows = size = 0
        with self._lock:
            if self._closed or cancelled.is_set():
                raise ReadCancelledError
            start = max(0, min(start, self.row_count))
            stop = min(max(start, stop), start + self.page_rows, self.row_count)
            index = max(0, bisect_right(self._starts, start) - 1)
            for batch_index in range(index, len(self._starts)):
                offset = self._starts[batch_index]
                if offset >= stop:
                    break
                if cancelled.is_set():
                    raise ReadCancelledError
                path = self.directory / f"{batch_index}.arrow"
                with path.open("rb") as file:
                    batch = ipc.open_file(file).get_batch(0)
                skip = max(0, start - offset)
                length = min(batch.num_rows - skip, stop - start - rows)
                batch = batch.slice(skip, length)
                keep = bounded_prefix(
                    batch, self.page_bytes - size, allow_one=rows == 0
                )
                if keep:
                    batch = batch.take(pa.array(range(keep), type=pa.int64()))
                    batches.append(batch)
                    rows += keep
                    size += batch.nbytes
                if keep < length or rows >= stop - start or size >= self.page_bytes:
                    break
        return DataPage(start, pa.Table.from_batches(batches, self.schema))

    def close(self) -> None:
        """Remove result files after any in-flight read or write releases the lock."""
        with self._lock:
            if not self._closed:
                self._temporary.cleanup()
                self._starts.clear()
                self._closed = True
