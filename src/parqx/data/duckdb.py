"""Worker-owned DuckDB connections, cancellation and bounded Arrow results."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread

import duckdb
import pyarrow as pa

from parqx.data.batch import bounded_prefix, compact_batch


class QueryCancelledError(Exception):
    """The caller cancelled this DuckDB operation."""


class QueryControl:
    """Cancel one operation safely from another thread, even before it starts."""

    def __init__(self, cancelled: Event | None = None) -> None:
        """Create cancellation and worker-lifecycle signals for one operation."""
        self.cancelled = cancelled if cancelled is not None else Event()
        self.started = Event()
        self.finished = Event()
        self._lock = Lock()
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._interrupt_stop = Event()
        self._interrupt_thread: Thread | None = None

    def cancel(self) -> None:
        """Record cancellation and interrupt until the connection detaches."""
        self.cancelled.set()
        with self._lock:
            if self._connection is not None:
                self._connection.interrupt()
                if self._interrupt_thread is None:
                    thread = Thread(
                        target=self._repeat_interrupt,
                        name="parqx-duckdb-cancel",
                        daemon=True,
                    )
                    # Start under the lock so detach cannot join an unstarted
                    # thread; only retain it if starting succeeds.
                    thread.start()
                    self._interrupt_thread = thread

    def _repeat_interrupt(self) -> None:
        # DuckDB clears early interrupts when starting a query. Keep retrying
        # across native entry until the worker detaches its connection.
        while not self._interrupt_stop.wait(0.01):
            with self._lock:
                if self._connection is None:
                    return
                self._connection.interrupt()

    def check(self) -> None:
        """Stop execution or reading if cancellation was requested."""
        if self.cancelled.is_set():
            raise QueryCancelledError

    def attach(self, connection: duckdb.DuckDBPyConnection) -> None:
        """Publish the worker-owned connection for interruption."""
        with self._lock:
            self.check()
            self._connection = connection

    def detach(self) -> None:
        """Stop interrupts before the worker closes its connection."""
        with self._lock:
            self._connection = None
            self._interrupt_stop.set()
            thread = self._interrupt_thread
        # The retry thread may need the lock before it can finish.
        if thread is not None:
            thread.join()


@dataclass(frozen=True)
class QueryPreview:
    """A bounded result; truncation does not limit the query's input."""

    table: pa.Table
    """Result rows retained within the preview budgets."""
    reason: str | None = None
    """Budget that stopped reading, or None for a complete result."""

    @property
    def truncated(self) -> bool:
        """Whether a preview budget stopped reading the result."""
        return self.reason is not None


@contextmanager
def connect(
    control: QueryControl, *, memory_limit: str = "256MB", threads: int = 2
) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """Own one connection and spill directory on the calling worker thread.

    Close Arrow readers inside this context. Call control.detach first if reader
    cleanup must also be protected from cross-thread interrupts.
    """
    control.check()
    with ExitStack() as resources:
        temporary = TemporaryDirectory(prefix="parqx-duckdb-")
        resources.callback(temporary.cleanup)
        connection = duckdb.connect(
            config={
                "threads": threads,
                "memory_limit": memory_limit,
                "temp_directory": temporary.name,
                "preserve_insertion_order": True,
                "python_enable_replacements": False,
            }
        )
        resources.callback(connection.close)
        try:
            control.attach(connection)
            yield connection
        finally:
            control.detach()


def read_preview(
    reader: pa.RecordBatchReader,
    control: QueryControl,
    *,
    row_limit: int,
    byte_limit: int,
) -> QueryPreview:
    """Read bounded Arrow rows, leaving reader cleanup to the caller.

    One oversized row is admitted. Decoder and engine allocations are separate
    from the retained preview budget.
    """
    control.check()
    schema = reader.schema
    batches: list[pa.RecordBatch] = []
    rows = size = 0
    reason: str | None = None
    while True:
        control.check()
        try:
            batch = reader.read_next_batch()
        except StopIteration:
            break
        control.check()
        if not batch.num_rows:
            continue
        available = min(batch.num_rows, row_limit - rows)
        if available <= 0 or size >= byte_limit:
            reason = "row limit" if available <= 0 else "byte budget"
            break
        keep = bounded_prefix(
            batch.slice(0, available), byte_limit - size, allow_one=rows == 0
        )
        if keep:
            # A slice could retain all the discarded rows' backing buffers.
            prefix = compact_batch(batch.slice(0, keep), copy=keep < batch.num_rows)
            batches.append(prefix)
            rows += keep
            size += prefix.nbytes
        if keep < batch.num_rows:
            reason = "row limit" if rows >= row_limit else "byte budget"
            break
    control.check()
    return QueryPreview(pa.Table.from_batches(batches, schema=schema), reason=reason)
