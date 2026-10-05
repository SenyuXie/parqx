"""Thread-owned DuckDB sessions with bounded Arrow previews and cancellation."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from glob import escape as escape_glob
from types import TracebackType
from typing import Self

import duckdb
import pyarrow as pa

from parqx.catalog import SourceIssue, SourceSpec
from parqx.data.duckdb import QueryCancelledError as QueryCancelledError
from parqx.data.duckdb import QueryControl as QueryControl
from parqx.data.duckdb import QueryPreview as QueryPreview
from parqx.data.duckdb import connect, read_preview


@dataclass(frozen=True)
class QueryLimits:
    """Execution and preview budgets, independent of the widget's render cache."""

    preview_rows: int = 10_000
    """Maximum number of result rows retained for display."""
    preview_bytes: int = 32 * 1024 * 1024
    """Retained Arrow byte budget, allowing one oversized row."""
    batch_rows: int = 1024
    """Number of rows requested per Arrow reader batch."""
    memory_limit: str = "256MB"
    """DuckDB execution memory limit, separate from preview data."""
    threads: int = 2
    """Maximum number of DuckDB execution threads."""

    def __post_init__(self) -> None:
        """Reject budgets which could prevent forward progress."""
        if (
            min(self.preview_rows, self.preview_bytes, self.batch_rows, self.threads)
            < 1
        ):
            raise ValueError("Query budgets must be positive")


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
        self._resources = ExitStack()
        self._reader: pa.RecordBatchReader | None = None

    def __enter__(self) -> Self:
        """Open a private connection and start one SELECT statement."""
        self.control.check()
        try:
            connection = self._resources.enter_context(
                connect(
                    self.control,
                    memory_limit=self.limits.memory_limit,
                    threads=self.limits.threads,
                )
            )
            statements = connection.extract_statements(self.sql)
            if (
                len(statements) != 1
                or statements[0].type != duckdb.StatementType.SELECT
            ):
                raise ValueError("Enter one SELECT query (WITH is supported).")
            self._register_sources(connection)
            self.control.check()
            relation = connection.sql(self.sql)
            self._reader = relation.to_arrow_reader(self.limits.batch_rows)
            self._resources.callback(self._reader.close)
            self.control.check()
        except BaseException:
            self.close()
            raise
        return self

    def _register_sources(self, connection: duckdb.DuckDBPyConnection) -> None:
        """Register readable files and retain file-specific failures as warnings."""
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

    def preview(self) -> QueryPreview:
        """Read a row- and byte-bounded preview, discarding the unconsumed tail.

        One oversized row is allowed so a result can always be inspected. Arrow
        decoding and DuckDB execution have separate allocations from this budget.
        """
        self.control.check()
        if self._reader is None:
            raise RuntimeError("Query session is not open")
        return read_preview(
            self._reader,
            self.control,
            row_limit=self.limits.preview_rows,
            byte_limit=self.limits.preview_bytes,
        )

    def close(self) -> None:
        """Release the reader, connection and engine spill directory."""
        # Stop cross-thread interrupts before closing resources in reverse order.
        self.control.detach()
        try:
            self._resources.close()
        finally:
            self._reader = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close on the worker thread regardless of how previewing ended."""
        self.close()
