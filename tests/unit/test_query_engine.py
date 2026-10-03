from dataclasses import replace
from os import name as os_name
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread, current_thread
from time import monotonic
from typing import cast
from unittest.mock import patch

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from parqx.data.batch import bounded_prefix
from parqx.data.catalog import SourceCatalog, SourceSpec
from parqx.query import engine
from parqx.query.engine import (
    QueryCancelledError,
    QueryControl,
    QueryLimits,
    QuerySession,
)


def sources(*paths: Path) -> tuple[SourceSpec, ...]:
    return tuple(entry.spec for entry in SourceCatalog(paths).entries)


def table_values(table: pa.Table) -> dict[str, list[object]]:
    return {
        name: [value.as_py() for value in table.column(name)]
        for name in table.column_names
    }


def test_query_file_with_quoted_path_and_cte(small_parquet: Path) -> None:
    path = small_parquet.with_name("a 'quoted' file.parquet")
    path.write_bytes(small_parquet.read_bytes())
    source = sources(path)[0]
    sql = (
        f"WITH x AS (SELECT * FROM {source.quoted_name} WHERE score > 2) "
        "SELECT id FROM x ORDER BY id DESC"
    )
    with QuerySession((source,), sql, QueryControl()) as session:
        result = session.preview()
    assert [v.as_py() for v in result.table.column(0)] == [5, 4, 3, 2]
    assert not result.truncated
    assert result.reason is None


@pytest.mark.parametrize(
    ("literal", "neighbor"),
    [
        ("items[1]", "items1"),
        ("left[", "left"),
        ("right]", "right"),
        ("parts*", "parts-extra"),
        ("问?号", "问1号"),
        ("all[*?]", "all*"),
    ],
)
def test_source_paths_are_literal_not_globs(
    tmp_path: Path, literal: str, neighbor: str
) -> None:
    if os_name == "nt" and any(char in literal for char in "*?"):
        pytest.skip("Windows filenames cannot contain * or ?")
    path = tmp_path / f"{literal}.parquet"
    pq.write_table(pa.table({"value": ["opened"]}), path)
    pq.write_table(pa.table({"value": ["unopened"]}), tmp_path / f"{neighbor}.parquet")
    inputs = sources(path)
    with QuerySession(
        inputs, f"SELECT * FROM {inputs[0].quoted_name}", QueryControl()
    ) as session:
        assert table_values(session.preview().table) == {"value": ["opened"]}
        assert not session.issues


def test_literal_parent_path_and_missing_source_cannot_match_neighbors(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "batch[1]"
    neighbor = tmp_path / "batch1"
    directory.mkdir()
    neighbor.mkdir()
    path = directory / "items[1].parquet"
    pq.write_table(pa.table({"value": ["opened"]}), path)
    for candidate in (directory / "items1.parquet", neighbor / "items1.parquet"):
        pq.write_table(pa.table({"value": ["unopened"]}), candidate)
    inputs = sources(path)
    with QuerySession(inputs, 'SELECT * FROM "items[1]"', QueryControl()) as session:
        assert table_values(session.preview().table) == {"value": ["opened"]}
    path.unlink()
    with QuerySession(inputs, "SELECT 42 AS answer", QueryControl()) as session:
        assert table_values(session.preview().table) == {"answer": [42]}
        assert len(session.issues) == 1
        assert session.issues[0].source == inputs[0]
    with (
        pytest.raises(duckdb.CatalogException),
        QuerySession(inputs, 'SELECT * FROM "items[1]"', QueryControl()),
    ):
        pass


def test_preview_limit_does_not_truncate_aggregation_input(small_parquet: Path) -> None:
    with QuerySession(
        sources(small_parquet),
        "SELECT count(*) AS n, sum(id) AS total FROM smoke",
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
        sources(small_parquet),
        "SELECT id FROM smoke ORDER BY id",
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
    sql = "SELECT id FROM smoke ORDER BY id"
    with QuerySession(sources(small_parquet), sql, QueryControl()) as session:
        complete = session.preview()
    for budget, truncated in [(16, True), (complete.table.nbytes, False)]:
        with QuerySession(
            sources(small_parquet),
            sql,
            QueryControl(),
            QueryLimits(preview_bytes=budget),
        ) as session:
            result = session.preview()
        assert result.table.num_rows >= 1
        assert result.table.nbytes <= budget
        assert result.truncated is truncated
        assert result.reason == ("byte budget" if truncated else None)


def test_one_oversized_value_is_still_available(small_parquet: Path) -> None:
    value = "x" * 100_000
    with QuerySession(
        sources(small_parquet),
        "SELECT repeat('x', 100000) AS value FROM smoke",
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
        sources(small_parquet),
        "SELECT CASE WHEN id = 1 THEN 'ok' ELSE repeat('x', 1000000) END AS value "
        "FROM smoke ORDER BY id",
        QueryControl(),
        QueryLimits(preview_rows=1, batch_rows=5),
    ) as session:
        result = session.preview()
    assert result.table.column(0)[0].as_py() == "ok"
    assert result.truncated
    assert result.table.get_total_buffer_size() < 1024


def test_empty_result_keeps_its_schema(small_parquet: Path) -> None:
    with QuerySession(
        sources(small_parquet), "SELECT id FROM smoke WHERE false", QueryControl()
    ) as session:
        result = session.preview()
    assert result.table.num_rows == 0
    assert result.table.column_names == ["id"]
    assert result.table.column(0).type == pa.int64()
    assert not result.truncated
    assert result.reason is None


@pytest.mark.parametrize(
    "sql", ["", "SELECT 1; SELECT 2", "CREATE TABLE x AS SELECT 1", "DELETE FROM smoke"]
)
def test_rejects_statements_without_a_single_query(
    small_parquet: Path, sql: str
) -> None:
    with (
        pytest.raises(ValueError, match="one SELECT"),
        QuerySession(sources(small_parquet), sql, QueryControl()),
    ):
        pass


def test_error_does_not_poison_next_session(small_parquet: Path) -> None:
    control = QueryControl()
    with (
        pytest.raises(duckdb.Error),
        QuerySession(sources(small_parquet), "SELECT missing FROM smoke", control),
    ):
        pass
    control.cancel()  # A closed connection must no longer receive interrupts.
    with QuerySession(sources(small_parquet), "SELECT 42", QueryControl()) as session:
        assert session.preview().table.column(0)[0].as_py() == 42


def test_cancel_before_start(small_parquet: Path) -> None:
    control = QueryControl()
    control.cancel()
    with (
        pytest.raises(QueryCancelledError),
        QuerySession(sources(small_parquet), "SELECT * FROM smoke", control),
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
            sources(small_parquet),
            "SELECT * FROM smoke",
            control,
            QueryLimits(batch_rows=1),
        ) as session,
        pytest.raises(QueryCancelledError),
    ):
        session.preview()
    control.cancel()


@pytest.mark.parametrize(
    "before_native", [True, False], ids=["before-native", "unpaused-entry"]
)
def test_cancel_interrupts_native_query(
    small_parquet: Path, monkeypatch: pytest.MonkeyPatch, before_native: bool
) -> None:
    ready_to_cancel = Event()
    enter_native = Event()
    query_finished = Event()
    errors: list[BaseException] = []
    original_reader = duckdb.DuckDBPyRelation.to_arrow_reader

    def start_reader(
        relation: duckdb.DuckDBPyRelation, batch_rows: int
    ) -> pa.RecordBatchReader:
        ready_to_cancel.set()
        if before_native:
            assert enter_native.wait(timeout=10), "Native-entry gate was not released"
        return original_reader(relation, batch_rows)

    monkeypatch.setattr(duckdb.DuckDBPyRelation, "to_arrow_reader", start_reader)
    control = QueryControl()

    def query() -> None:
        try:
            with QuerySession(
                sources(small_parquet),
                "SELECT sum(sin(i)) FROM range(1000000000) t(i)",
                control,
            ) as session:
                session.preview()
        except BaseException as exc:
            errors.append(exc)
        finally:
            query_finished.set()

    worker = Thread(target=query, daemon=True)
    worker.start()
    try:
        assert ready_to_cancel.wait(timeout=10), (
            "DuckDB did not reach cancellation point"
        )
        assert worker.is_alive(), "The query finished before cancellation"
        control.cancel()
        enter_native.set()
        assert query_finished.wait(timeout=5), (
            "DuckDB did not stop after one cancellation"
        )
        worker.join(timeout=1)
        assert not worker.is_alive(), "Query worker did not exit"
        assert len(errors) == 1
        assert isinstance(errors[0], duckdb.InterruptException)
    finally:
        # Cleanup happens only after the single-cancel assertion has passed or
        # failed. A second interrupt here cannot make a lost-cancel test pass.
        enter_native.set()
        deadline = monotonic() + 5
        while worker.is_alive() and monotonic() < deadline:
            control.cancel()
            worker.join(timeout=0.01)

    with QuerySession(sources(small_parquet), "SELECT 42", QueryControl()) as session:
        assert session.preview().table.column(0)[0].as_py() == 42


def test_completed_query_does_not_start_interrupt_thread(small_parquet: Path) -> None:
    with patch.object(engine, "Thread", wraps=Thread) as create_thread:
        control = QueryControl()
        with QuerySession(sources(small_parquet), "SELECT 42", control) as session:
            assert session.preview().table.column(0)[0].as_py() == 42
        control.cancel()
        create_thread.assert_not_called()


def test_repeated_cancel_uses_one_thread_and_detach_joins_it() -> None:
    caller = current_thread()
    retried = Event()
    interrupts: list[Thread] = []

    class Connection:
        closed = False

        def interrupt(self) -> None:
            assert not self.closed, "An interrupt reached the closed connection"
            thread = current_thread()
            interrupts.append(thread)
            if thread is not caller:
                retried.set()

    connection = Connection()
    control = QueryControl()
    control.attach(cast(duckdb.DuckDBPyConnection, connection))
    with patch.object(engine, "Thread", wraps=Thread) as create_thread:
        try:
            control.cancel()
            retry_thread = control._interrupt_thread  # pyright: ignore[reportPrivateUsage]
            assert retry_thread is not None
            assert retried.wait(timeout=2), "Cancellation did not retry interruption"
            control.cancel()
            control.cancel()
            create_thread.assert_called_once()
            assert control._interrupt_thread is retry_thread  # pyright: ignore[reportPrivateUsage]
            with patch.object(retry_thread, "join", wraps=retry_thread.join) as join:
                control.detach()
                join.assert_called_once()
            assert not retry_thread.is_alive()

            connection.closed = True
            count = len(interrupts)
            control.cancel()
            assert len(interrupts) == count
            create_thread.assert_called_once()
        finally:
            control.detach()


@pytest.mark.parametrize("sql", ["SELECT * FROM smoke", "SELECT missing FROM smoke"])
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
        with QuerySession(sources(small_parquet), sql, QueryControl()) as session:
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


def test_join_and_three_source_cte(small_parquet: Path, tmp_path: Path) -> None:
    orders = tmp_path / "orders.parquet"
    products = tmp_path / "products.parquet"
    pq.write_table(
        pa.table(
            {"user_id": [1, 3, 1], "product_id": [8, 9, 9], "quantity": [2, 1, 3]}
        ),
        orders,
    )
    pq.write_table(pa.table({"id": [8, 9], "price": [5, 7]}), products)
    inputs = sources(small_parquet, orders, products)
    with QuerySession(
        inputs,
        "SELECT s.name, o.quantity FROM smoke s JOIN orders o ON s.id = o.user_id "
        "ORDER BY s.id, o.quantity",
        QueryControl(),
    ) as session:
        assert table_values(session.preview().table) == {
            "name": ["alice", "alice", "carol"],
            "quantity": [2, 3, 1],
        }
    with QuerySession(
        inputs,
        "WITH totals AS ("
        "SELECT s.id, sum(o.quantity * p.price) AS cost FROM smoke s "
        "JOIN orders o ON s.id = o.user_id "
        "JOIN products p ON p.id = o.product_id GROUP BY s.id"
        ") SELECT * FROM totals ORDER BY id",
        QueryControl(),
    ) as session:
        assert table_values(session.preview().table) == {"id": [1, 3], "cost": [31, 7]}
        assert session.sources == inputs
        assert session.issues == ()


def test_union_and_preview_limit_preserve_full_multi_source_input(
    small_parquet: Path, tmp_path: Path
) -> None:
    extra = tmp_path / "extra.parquet"
    pq.write_table(pa.table({"value": [10, 20]}), extra)
    with QuerySession(
        sources(small_parquet, extra),
        "WITH all_values AS (SELECT id AS value FROM smoke "
        "UNION ALL SELECT value FROM extra) "
        "SELECT count(*) AS n, sum(value) AS total FROM all_values",
        QueryControl(),
        QueryLimits(preview_rows=1),
    ) as session:
        preview = session.preview()
        assert table_values(preview.table) == {"n": [7], "total": [45]}
        assert not preview.truncated


@pytest.mark.parametrize("invalid_file", [False, True])
def test_unavailable_source_warns_without_blocking_other_queries(
    small_parquet: Path, tmp_path: Path, invalid_file: bool
) -> None:
    unavailable = tmp_path / "unavailable.parquet"
    if invalid_file:
        unavailable.write_text("Not a Parquet file")
    inputs = sources(small_parquet, unavailable)
    with QuerySession(
        inputs, "SELECT count(*) AS n FROM smoke", QueryControl()
    ) as session:
        assert table_values(session.preview().table) == {"n": [5]}
        assert len(session.issues) == 1
        assert session.issues[0].source == inputs[1]
        assert str(unavailable) in str(session.issues[0])
        assert session.issues[0].message

    failed = QuerySession(inputs, "SELECT * FROM unavailable", QueryControl())
    with pytest.raises(duckdb.CatalogException, match="unavailable"), failed:
        pass
    assert len(failed.issues) == 1
    assert failed.issues[0].source == inputs[1]

    unavailable.write_bytes(small_parquet.read_bytes())
    with QuerySession(
        inputs, "SELECT count(*) AS n FROM unavailable", QueryControl()
    ) as recovered:
        assert table_values(recovered.preview().table) == {"n": [5]}
        assert recovered.issues == ()


def test_no_implicit_data_alias_but_actual_data_file_is_available(
    small_parquet: Path, tmp_path: Path
) -> None:
    with (
        pytest.raises(duckdb.CatalogException, match="data"),
        QuerySession(sources(small_parquet), "SELECT * FROM data", QueryControl()),
    ):
        pass
    path = tmp_path / "data.parquet"
    path.write_bytes(small_parquet.read_bytes())
    with QuerySession(
        sources(path), "SELECT count(*) AS n FROM data", QueryControl()
    ) as session:
        assert table_values(session.preview().table) == {"n": [5]}


@pytest.mark.parametrize(
    "name", ["two words", "销售记录", "order-items", "many.dots", 'a"b', "select"]
)
def test_sql_identifiers_are_registered_without_rewriting(
    small_parquet: Path, name: str
) -> None:
    source = replace(sources(small_parquet)[0], table_name=name)
    with QuerySession(
        (source,), f"SELECT count(*) AS n FROM {source.quoted_name}", QueryControl()
    ) as session:
        assert table_values(session.preview().table) == {"n": [5]}


def test_query_without_sources() -> None:
    with QuerySession((), "SELECT 42 AS answer", QueryControl()) as session:
        assert table_values(session.preview().table) == {"answer": [42]}
        assert session.sources == ()
        assert session.issues == ()


def test_python_variables_are_not_implicit_query_sources() -> None:
    replacement_input = pa.table({"value": [42]})
    assert replacement_input.num_rows == 1
    # Confirm that this variable would be visible with DuckDB's default setting.
    with duckdb.connect() as connection:
        assert connection.sql("SELECT * FROM replacement_input").fetchone() == (42,)
    with (
        pytest.raises(duckdb.CatalogException, match="replacement_input"),
        QuerySession((), "SELECT * FROM replacement_input", QueryControl()),
    ):
        pass


@pytest.mark.parametrize(
    ("operation", "failure"),
    [
        ("read_parquet", duckdb.OutOfMemoryException("memory budget")),
        ("read_parquet", duckdb.InterruptException("cancelled")),
        ("create_view", duckdb.InvalidInputException("invalid view")),
        ("create_view", duckdb.CatalogException("name collision")),
    ],
)
def test_fatal_registration_errors_propagate_and_close_resources(
    small_parquet: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure: duckdb.Error,
) -> None:
    directories: list[Path] = []

    def create_temporary(*, prefix: str) -> TemporaryDirectory[str]:
        temporary = TemporaryDirectory(prefix=prefix, dir=tmp_path)
        directories.append(Path(temporary.name))
        return temporary

    monkeypatch.setattr(engine, "TemporaryDirectory", create_temporary)
    owner = (
        duckdb.DuckDBPyConnection
        if operation == "read_parquet"
        else duckdb.DuckDBPyRelation
    )
    control = QueryControl()
    session = QuerySession(sources(small_parquet), "SELECT 42", control)
    original_close = duckdb.DuckDBPyConnection.close
    closed: list[duckdb.DuckDBPyConnection] = []

    def close(connection: duckdb.DuckDBPyConnection) -> None:
        closed.append(connection)
        original_close(connection)

    monkeypatch.setattr(duckdb.DuckDBPyConnection, "close", close)
    with (
        patch.object(owner, operation, side_effect=failure),
        pytest.raises(type(failure), match=str(failure)),
        session,
    ):
        pass
    assert session.issues == ()
    assert len(closed) == 1
    assert len(directories) == 1
    assert not directories[0].exists()
    control.cancel()


def test_cancellation_during_registration_is_not_a_source_warning(
    small_parquet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = QueryControl()

    def cancel_during_read(
        _connection: duckdb.DuckDBPyConnection, _path: str
    ) -> duckdb.DuckDBPyRelation:
        control.cancel()
        raise duckdb.IOException("Interrupted while opening")

    monkeypatch.setattr(duckdb.DuckDBPyConnection, "read_parquet", cancel_during_read)
    session = QuerySession(sources(small_parquet), "SELECT 42", control)
    with pytest.raises(QueryCancelledError), session:
        pass
    assert session.issues == ()
    control.cancel()


def test_registration_does_not_replace_a_prior_view(small_parquet: Path) -> None:
    source = sources(small_parquet)[0]
    with (
        pytest.raises(duckdb.CatalogException, match="already exists"),
        QuerySession((source, source), "SELECT 42", QueryControl()),
    ):
        pass
