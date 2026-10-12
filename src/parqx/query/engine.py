"""Thread-owned SQL execution with bounded Arrow previews."""

from __future__ import annotations

import logging
from contextlib import closing
from dataclasses import dataclass
from time import perf_counter

import duckdb
import pyarrow as pa

from parqx.catalog import SourceIssue, SourceSpec
from parqx.config import ParqxConfig
from parqx.data.duckdb import connect, read_preview

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueryResult:
    """A completed SQL preview, timing and source availability warnings."""

    sql: str
    table: pa.Table
    elapsed: float
    issues: tuple[SourceIssue, ...] = ()
    truncation_reason: str | None = None


class QueryExecutionError(Exception):
    """A failed query with source warnings collected before the failure."""

    def __init__(self, message: str, issues: tuple[SourceIssue, ...] = ()) -> None:
        """Keep the display message and partial source issues together."""
        super().__init__(message)
        self.issues = issues


def execute_query(
    sources: tuple[SourceSpec, ...],
    sql: str,
    *,
    config: ParqxConfig,
    unavailable: tuple[SourceIssue, ...] = (),
) -> QueryResult:
    """Execute one SELECT and release native resources on the calling thread."""
    started = perf_counter()
    issues = list(unavailable)
    try:
        with connect(
            memory_limit=config.memory_limit, threads=config.threads
        ) as connection:
            statements = connection.extract_statements(sql)
            if (
                len(statements) != 1
                or statements[0].type != duckdb.StatementType.SELECT
            ):
                raise ValueError("Enter one SELECT query (WITH is supported).")
            for source in sources:
                try:
                    relation = connection.read_parquet(str(source.path))
                except (
                    duckdb.IOException,
                    duckdb.InvalidInputException,
                    duckdb.PermissionException,
                    OSError,
                ) as exc:
                    issues.append(SourceIssue(source, str(exc)))
                    continue
                relation.create_view(source.table_name, replace=False)
            relation = connection.sql(sql)
            with closing(relation.to_arrow_reader(config.query_batch_rows)) as reader:
                table, reason = read_preview(
                    reader,
                    row_limit=config.preview_rows,
                    byte_limit=config.preview_bytes,
                )
        return QueryResult(sql, table, perf_counter() - started, tuple(issues), reason)
    except Exception as exc:
        if not isinstance(
            exc, (duckdb.Error, pa.ArrowException, OSError, ValueError, MemoryError)
        ):
            logger.exception("Unexpected query failure")
        raise QueryExecutionError(
            str(exc) or type(exc).__name__, tuple(issues)
        ) from exc
