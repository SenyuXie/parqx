from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic

import duckdb
import pyarrow as pa
import pytest

from parqx.data.batch import bounded_prefix
from parqx.query import engine
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
    assert result.reason is None


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
    ("row_limit", "batch_rows", "expected_rows", "truncated"),
    [(3, 2, 3, True), (2, 4, 2, True), (4, 2, 4, True), (5, 2, 5, False)],
)
def test_row_budget_boundaries(
    small_parquet: Path,
    row_limit: int,
    batch_rows: int,
    expected_rows: int,
    truncated: bool,
) -> None:
    with QuerySession(
        small_parquet,
        "SELECT id FROM data ORDER BY id",
        QueryControl(),
        QueryLimits(preview_rows=row_limit, batch_rows=batch_rows),
    ) as session:
        result = session.preview()
    assert [value.as_py() for value in result.table.column(0)] == list(
        range(1, expected_rows + 1)
    )
    assert result.truncated is truncated
    assert result.reason == ("row limit" if truncated else None)


def test_byte_budget_and_exact_result_boundary(small_parquet: Path) -> None:
    sql = "SELECT id FROM data ORDER BY id"
    with QuerySession(small_parquet, sql, QueryControl()) as session:
        complete = session.preview()
    for budget, truncated in [(16, True), (complete.table.nbytes, False)]:
        with QuerySession(
            small_parquet, sql, QueryControl(), QueryLimits(preview_bytes=budget)
        ) as session:
            result = session.preview()
        assert result.table.num_rows >= 1
        assert result.table.nbytes <= budget
        assert result.truncated is truncated
        assert result.reason == ("byte budget" if truncated else None)


def test_one_oversized_value_is_still_available(small_parquet: Path) -> None:
    value = "x" * 100_000
    with QuerySession(
        small_parquet,
        "SELECT repeat('x', 100000) AS value FROM data",
        QueryControl(),
        QueryLimits(preview_bytes=16),
    ) as session:
        result = session.preview()
    assert result.table.num_rows == 1
    assert result.table.column(0)[0].as_py() == value
    assert result.table.nbytes > 16
    assert result.truncated
    assert result.reason == "byte budget"


def test_truncated_prefix_does_not_retain_discarded_buffers(
    small_parquet: Path,
) -> None:
    with QuerySession(
        small_parquet,
        "SELECT CASE WHEN id = 1 THEN 'ok' ELSE repeat('x', 1000000) END AS value "
        "FROM data ORDER BY id",
        QueryControl(),
        QueryLimits(preview_rows=1, batch_rows=5),
    ) as session:
        result = session.preview()
    assert result.table.column(0)[0].as_py() == "ok"
    assert result.truncated
    assert result.table.get_total_buffer_size() < 1024


def test_empty_result_keeps_its_schema(small_parquet: Path) -> None:
    with QuerySession(
        small_parquet, "SELECT id FROM data WHERE false", QueryControl()
    ) as session:
        result = session.preview()
    assert result.table.num_rows == 0
    assert result.table.column_names == ["id"]
    assert result.table.column(0).type == pa.int64()
    assert not result.truncated
    assert result.reason is None


@pytest.mark.parametrize(
    "sql", ["", "SELECT 1; SELECT 2", "CREATE TABLE x AS SELECT 1", "DELETE FROM data"]
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
    control = QueryControl()
    with (
        pytest.raises(duckdb.Error),
        QuerySession(small_parquet, "SELECT missing FROM data", control),
    ):
        pass
    control.cancel()  # A closed connection must no longer receive interrupts.
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


def test_cancel_during_preview_and_after_close(
    small_parquet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = QueryControl()

    def cancel_after_first_batch(
        batch: pa.RecordBatch, budget: int, *, allow_one: bool
    ) -> int:
        control.cancel()
        return bounded_prefix(batch, budget, allow_one=allow_one)

    monkeypatch.setattr(engine, "bounded_prefix", cancel_after_first_batch)
    with (
        QuerySession(
            small_parquet, "SELECT * FROM data", control, QueryLimits(batch_rows=1)
        ) as session,
        pytest.raises(QueryCancelledError),
    ):
        session.preview()
    control.cancel()


def test_cancel_interrupts_native_query(
    small_parquet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    query_started = Event()
    query_finished = Event()
    errors: list[BaseException] = []
    original_reader = duckdb.DuckDBPyRelation.to_arrow_reader

    def start_reader(
        relation: duckdb.DuckDBPyRelation, batch_rows: int
    ) -> pa.RecordBatchReader:
        query_started.set()
        return original_reader(relation, batch_rows)

    monkeypatch.setattr(duckdb.DuckDBPyRelation, "to_arrow_reader", start_reader)
    control = QueryControl()

    def query() -> None:
        try:
            with QuerySession(
                small_parquet, "SELECT sum(sin(i)) FROM range(1000000000) t(i)", control
            ) as session:
                session.preview()
        except BaseException as exc:
            errors.append(exc)
        finally:
            query_finished.set()

    worker = Thread(target=query, daemon=True)
    worker.start()
    try:
        assert query_started.wait(timeout=10), "DuckDB did not start executing"
        assert worker.is_alive(), "The query finished before cancellation"
        # The wrapper signals immediately before the real native call. Reissue
        # interrupts so one arriving before native entry cannot be lost.
        deadline = monotonic() + 5
        while not query_finished.is_set() and monotonic() < deadline:
            control.cancel()
            query_finished.wait(timeout=0.01)
        worker.join(timeout=1)
        assert not worker.is_alive(), "DuckDB did not stop after interruption"
        assert len(errors) == 1
        assert isinstance(errors[0], duckdb.InterruptException)
    finally:
        control.cancel()
        worker.join(timeout=5)

    with QuerySession(small_parquet, "SELECT 42", QueryControl()) as session:
        assert session.preview().table.column(0)[0].as_py() == 42


@pytest.mark.parametrize("sql", ["SELECT * FROM data", "SELECT missing FROM data"])
def test_session_cleans_spill_directory_on_success_or_error(
    small_parquet: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sql: str
) -> None:
    directories: list[Path] = []

    def create_temporary(*, prefix: str) -> TemporaryDirectory[str]:
        temporary = TemporaryDirectory(prefix=prefix, dir=tmp_path)
        directories.append(Path(temporary.name))
        return temporary

    monkeypatch.setattr(engine, "TemporaryDirectory", create_temporary)
    try:
        with QuerySession(small_parquet, sql, QueryControl()) as session:
            session.preview()
            assert directories[0].exists()
    except duckdb.BinderException:
        assert "missing" in sql
    assert len(directories) == 1
    assert not directories[0].exists()


@pytest.mark.parametrize(
    ("rows", "size", "batch", "threads"),
    [(0, 1, 1, 1), (1, 0, 1, 1), (1, 1, 0, 1), (1, 1, 1, 0)],
)
def test_rejects_nonpositive_budgets(
    rows: int, size: int, batch: int, threads: int
) -> None:
    with pytest.raises(ValueError, match="positive"):
        QueryLimits(
            preview_rows=rows, preview_bytes=size, batch_rows=batch, threads=threads
        )
