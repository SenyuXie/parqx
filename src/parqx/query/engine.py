"""Thread-owned DuckDB sessions with bounded Arrow previews and cancellation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
from types import TracebackType
from typing import Self

import duckdb
import pyarrow as pa

from parqx.data.batch import bounded_prefix


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
        """Create a cancellation signal for one query only."""
        self.cancelled = Event()
        self.load_all = Event()
        self.started = Event()
        self.finished = Event()
        self._lock = Lock()
        self._connection: duckdb.DuckDBPyConnection | None = None

    def cancel(self) -> None:
        """Signal cancellation and interrupt a currently executing query."""
        self.cancelled.set()
        self.load_all.set()
        with self._lock:
            if self._connection is not None:
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
        """Prevent interruption racing with connection cleanup."""
        with self._lock:
            self._connection = None


@dataclass(frozen=True)
class QueryPreview:
    """A bounded initial result; truncation does not limit the query's input."""

    table: pa.Table
    truncated: bool
    reason: str | None = None


class QuerySession:
    """Execute once and consume Arrow batches on the owning worker thread.

    The reader remains alive after a truncated preview so the same execution can
    continue into a disk-backed result store. Close the session on its owner
    thread; only QueryControl.cancel is intended for cross-thread access.
    """

    def __init__(
        self,
        path: Path,
        sql: str,
        control: QueryControl,
        limits: QueryLimits | None = None,
    ) -> None:
        """Record the source and SQL without performing I/O."""
        self.path = path
        self.sql = sql
        self.control = control
        self.limits = limits or QueryLimits()
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._reader: pa.RecordBatchReader | None = None
        self._temporary: TemporaryDirectory[str] | None = None
        self._pending: pa.RecordBatch | None = None
        self.exhausted = False

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
            connection.read_parquet(str(self.path.resolve())).create_view("data")
            relation = connection.sql(self.sql)
            self._reader = relation.to_arrow_reader(self.limits.batch_rows)
            self.control.check()
        except BaseException:
            self.close()
            raise
        return self

    @property
    def schema(self) -> pa.Schema:
        """The query output schema, including for empty results."""
        if self._reader is None:
            raise RuntimeError("Query session is not open")
        return self._reader.schema

    def read_batch(self) -> pa.RecordBatch | None:
        """Read the next batch without rerunning the query."""
        self.control.check()
        if self._pending is not None:
            batch, self._pending = self._pending, None
            return batch
        if self.exhausted:
            return None
        if self._reader is None:
            raise RuntimeError("Query session is not open")
        try:
            batch = self._reader.read_next_batch()
        except StopIteration:
            self.exhausted = True
            return None
        self.control.check()
        return batch

    def preview(self) -> QueryPreview:
        """Read a row- and byte-bounded preview, retaining the unconsumed tail.

        One oversized row is allowed so a result can always be inspected. Arrow
        decoding and DuckDB execution have separate allocations from this budget.
        """
        batches: list[pa.RecordBatch] = []
        rows = size = 0
        reason: str | None = None
        while (batch := self.read_batch()) is not None:
            if not batch.num_rows:
                continue
            available = min(batch.num_rows, self.limits.preview_rows - rows)
            if available <= 0 or size >= self.limits.preview_bytes:
                self._pending = batch
                reason = "row limit" if available <= 0 else "byte budget"
                break
            keep = bounded_prefix(
                batch.slice(0, available),
                self.limits.preview_bytes - size,
                allow_one=rows == 0,
            )
            if keep:
                prefix = (
                    batch
                    if keep == batch.num_rows
                    else batch.take(pa.array(range(keep), type=pa.int64()))
                )
                batches.append(prefix)
                rows += keep
                size += prefix.nbytes
            if keep < batch.num_rows:
                self._pending = batch.slice(keep)
                reason = (
                    "row limit" if rows >= self.limits.preview_rows else "byte budget"
                )
                break
        return QueryPreview(
            pa.Table.from_batches(batches, schema=self.schema),
            truncated=reason is not None,
            reason=reason,
        )

    def close(self) -> None:
        """Release the reader, connection and engine spill directory."""
        self.control.detach()
        try:
            if self._reader is not None:
                self._reader.close()
        finally:
            self._reader = None
            self._pending = None
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
        """Close on the worker thread regardless of how consumption ended."""
        self.close()
