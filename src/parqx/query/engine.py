"""Thread-owned DuckDB sessions with bounded Arrow previews and cancellation."""

from __future__ import annotations

from dataclasses import dataclass
from glob import escape as escape_glob
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from types import TracebackType
from typing import Self

import duckdb
import pyarrow as pa

from parqx.data.batch import bounded_prefix, compact_batch
from parqx.data.catalog import SourceIssue, SourceSpec


class QueryCancelledError(Exception):
    """The user cancelled this query."""


@dataclass(frozen=True)
class QueryLimits:
    """Execution and preview budgets, independent of the widget's render cache."""

    preview_rows: int = 10_000
    preview_bytes: int = 32 * 1024 * 1024
    batch_rows: int = 1024
    memory_limit: str = "256MB"
    threads: int = 2

    def __post_init__(self) -> None:
        """Reject budgets which could prevent forward progress."""
        if (
            min(self.preview_rows, self.preview_bytes, self.batch_rows, self.threads)
            < 1
        ):
            raise ValueError("Query budgets must be positive")


class QueryControl:
    """Cancel a session safely from another thread, including before it starts."""

    def __init__(self) -> None:
        """Create cancellation and worker-lifecycle signals for one query only."""
        self.cancelled = Event()
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
                        name="parqx-query-cancel",
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
    reason: str | None = None

    @property
    def truncated(self) -> bool:
        """Whether a preview budget stopped reading the result."""
        return self.reason is not None


class QuerySession:
    """Execute and read one preview on the owning worker thread.

    Leave the context after previewing to release the reader and connection.
    Only QueryControl.cancel is intended for cross-thread access.
    """

    def __init__(
        self,
        sources: tuple[SourceSpec, ...],
        sql: str,
        control: QueryControl,
        limits: QueryLimits | None = None,
    ) -> None:
        """Record the source and SQL without performing I/O."""
        self.sources = sources
        self.issues: tuple[SourceIssue, ...] = ()
        self.sql = sql
        self.control = control
        self.limits = limits or QueryLimits()
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._reader: pa.RecordBatchReader | None = None
        self._temporary: TemporaryDirectory[str] | None = None

    def __enter__(self) -> Self:
        """Open a private connection and start one SELECT statement."""
        self.control.check()
        self._temporary = TemporaryDirectory(prefix="parqx-duckdb-")
        try:
            connection = duckdb.connect(
                config={
                    "threads": self.limits.threads,
                    "memory_limit": self.limits.memory_limit,
                    "temp_directory": self._temporary.name,
                    "python_enable_replacements": False,
                }
            )
            self._connection = connection
            self.control.attach(connection)
            statements = connection.extract_statements(self.sql)
            if (
                len(statements) != 1
                or statements[0].type != duckdb.StatementType.SELECT
            ):
                raise ValueError("Enter one SELECT query (WITH is supported).")
            for source in self.sources:
                self.control.check()
                try:
                    # DuckDB expands glob syntax even for one path. A source
                    # must read exactly its registered file, including []?*.
                    relation = connection.read_parquet(escape_glob(str(source.path)))
                except (
                    duckdb.IOException,
                    duckdb.InvalidInputException,
                    duckdb.PermissionException,
                    OSError,
                ) as exc:
                    self.control.check()
                    self.issues += (SourceIssue(source, str(exc)),)
                    continue
                self.control.check()
                relation.create_view(source.table_name, replace=False)
                self.control.check()
            self.control.check()
            relation = connection.sql(self.sql)
            self._reader = relation.to_arrow_reader(self.limits.batch_rows)
            self.control.check()
        except BaseException:
            self.close()
            raise
        return self

    def preview(self) -> QueryPreview:
        """Read a row- and byte-bounded preview, discarding the unconsumed tail.

        One oversized row is allowed so a result can always be inspected. Arrow
        decoding and DuckDB execution have separate allocations from this budget.
        """
        self.control.check()
        if self._reader is None:
            raise RuntimeError("Query session is not open")
        schema = self._reader.schema
        batches: list[pa.RecordBatch] = []
        rows = size = 0
        reason: str | None = None
        while True:
            self.control.check()
            try:
                batch = self._reader.read_next_batch()
            except StopIteration:
                break
            self.control.check()
            if not batch.num_rows:
                continue
            available = min(batch.num_rows, self.limits.preview_rows - rows)
            if available <= 0 or size >= self.limits.preview_bytes:
                reason = "row limit" if available <= 0 else "byte budget"
                break
            keep = bounded_prefix(
                batch.slice(0, available),
                self.limits.preview_bytes - size,
                allow_one=rows == 0,
            )
            if keep:
                # A slice could retain all the discarded rows' backing buffers.
                prefix = compact_batch(batch.slice(0, keep), copy=keep < batch.num_rows)
                batches.append(prefix)
                rows += keep
                size += prefix.nbytes
            if keep < batch.num_rows:
                reason = (
                    "row limit" if rows >= self.limits.preview_rows else "byte budget"
                )
                break
        self.control.check()
        return QueryPreview(
            pa.Table.from_batches(batches, schema=schema), reason=reason
        )

    def close(self) -> None:
        """Release the reader, connection and engine spill directory."""
        self.control.detach()
        try:
            if self._reader is not None:
                self._reader.close()
        finally:
            self._reader = None
            try:
                if self._connection is not None:
                    self._connection.close()
            finally:
                self._connection = None
                if self._temporary is not None:
                    self._temporary.cleanup()
                    self._temporary = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close on the worker thread regardless of how previewing ended."""
        self.close()
