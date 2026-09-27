from pathlib import Path

import duckdb
import pytest

from parqx.query.engine import (
    QueryCancelledError,
    QueryControl,
    QueryLimits,
    QuerySession,
)


def test_query_file_with_quoted_path_and_cte(small_parquet: Path) -> None:
    path = small_parquet.with_name("a 'quoted' file.parquet")
    path.write_bytes(small_parquet.read_bytes())
    sql = "WITH x AS (SELECT * FROM data WHERE score > 2) SELECT id FROM x ORDER BY id DESC"
    with QuerySession(path, sql, QueryControl()) as session:
        result = session.preview()
    assert [v.as_py() for v in result.table.column(0)] == [5, 4, 3, 2]
    assert not result.truncated


def test_preview_limit_does_not_truncate_aggregation_input(small_parquet: Path) -> None:
    with QuerySession(
        small_parquet,
        "SELECT count(*) AS n, sum(id) AS total FROM data",
        QueryControl(),
        QueryLimits(preview_rows=1),
    ) as session:
        result = session.preview()
    assert result.table.column(0)[0].as_py() == 5
    assert result.table.column(1)[0].as_py() == 15
    assert not result.truncated


@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        (QueryLimits(preview_rows=3, batch_rows=2), "row limit"),
        (QueryLimits(preview_rows=2, preview_bytes=1024, batch_rows=4), "row limit"),
        (QueryLimits(preview_rows=3, preview_bytes=16, batch_rows=4), "byte budget"),
        (QueryLimits(preview_rows=3, preview_bytes=1, batch_rows=4), "byte budget"),
    ],
)
def test_preview_resumes_the_same_result_without_gaps(
    small_parquet: Path, limits: QueryLimits, reason: str
) -> None:
    with QuerySession(
        small_parquet, "SELECT id FROM data ORDER BY id", QueryControl(), limits
    ) as session:
        result = session.preview()
        rows = [v.as_py() for v in result.table.column(0)]
        assert result.truncated
        assert result.reason == reason
        while (batch := session.read_batch()) is not None:
            rows.extend(v.as_py() for v in batch.column(0))
        assert rows == [1, 2, 3, 4, 5]
        assert session.exhausted


def test_byte_budget_and_single_oversized_value(small_parquet: Path) -> None:
    for budget, expected_rows in [(16, 2), (1, 1)]:
        with QuerySession(
            small_parquet,
            "SELECT id FROM data ORDER BY id",
            QueryControl(),
            QueryLimits(preview_bytes=budget),
        ) as session:
            result = session.preview()
            assert 1 <= result.table.num_rows <= expected_rows
            assert result.truncated
            assert result.reason == "byte budget"


def test_empty_result_keeps_its_schema(small_parquet: Path) -> None:
    with QuerySession(
        small_parquet, "SELECT id FROM data WHERE false", QueryControl()
    ) as session:
        result = session.preview()
    assert result.table.num_rows == 0
    assert result.table.column_names == ["id"]
    assert not result.truncated


@pytest.mark.parametrize(
    "sql", ["", "SELECT 1; SELECT 2", "CREATE TABLE x AS SELECT 1"]
)
def test_rejects_statements_without_a_single_query(
    small_parquet: Path, sql: str
) -> None:
    with (
        pytest.raises(ValueError, match="one SELECT"),
        QuerySession(small_parquet, sql, QueryControl()),
    ):
        pass


def test_error_does_not_poison_next_session(small_parquet: Path) -> None:
    with (
        pytest.raises(duckdb.Error),
        QuerySession(small_parquet, "SELECT missing FROM data", QueryControl()),
    ):
        pass
    with QuerySession(small_parquet, "SELECT 42", QueryControl()) as session:
        assert session.preview().table.column(0)[0].as_py() == 42


def test_cancel_before_start(small_parquet: Path) -> None:
    control = QueryControl()
    control.cancel()
    with (
        pytest.raises(QueryCancelledError),
        QuerySession(small_parquet, "SELECT * FROM data", control),
    ):
        pass


def test_cancel_during_read_and_after_close(small_parquet: Path) -> None:
    control = QueryControl()
    with QuerySession(small_parquet, "SELECT * FROM data", control) as session:
        control.cancel()
        with pytest.raises(QueryCancelledError):
            session.read_batch()
    control.cancel()
