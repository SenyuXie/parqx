"""SQL behavior and resource ownership at the execution/native boundary."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from parqx.catalog import SourceCatalog, SourceIssue, SourceSpec
from parqx.config import ParqxConfig
from parqx.data import duckdb as engine
from parqx.query.engine import QueryExecutionError, execute_query


def sources(*paths: Path) -> tuple[SourceSpec, ...]:
    return tuple(entry.spec for entry in SourceCatalog(paths).entries)


@pytest.mark.parametrize(
    "sql", ["", "SELECT 1; SELECT 2", "CREATE TABLE x AS SELECT 1"]
)
def test_rejects_statements_without_a_single_query(sql: str) -> None:
    with pytest.raises(QueryExecutionError, match="one SELECT") as raised:
        execute_query((), sql, config=ParqxConfig())
    assert isinstance(raised.value.__cause__, ValueError)


def test_error_does_not_poison_next_execution(small_parquet: Path) -> None:
    with pytest.raises(QueryExecutionError) as raised:
        execute_query(
            sources(small_parquet), "SELECT missing FROM smoke", config=ParqxConfig()
        )
    assert isinstance(raised.value.__cause__, duckdb.BinderException)
    result = execute_query(sources(small_parquet), "SELECT 42", config=ParqxConfig())
    assert result.table.column(0)[0].as_py() == 42
    assert result.sql == "SELECT 42"
    assert result.elapsed >= 0


@pytest.mark.parametrize("failure", [RuntimeError("unexpected"), MemoryError()])
def test_native_failure_is_wrapped_and_unexpected_error_logged_once(
    failure: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch.object(duckdb, "connect", side_effect=failure),
        pytest.raises(QueryExecutionError) as raised,
    ):
        execute_query((), "SELECT 42", config=ParqxConfig())
    assert str(raised.value) == (str(failure) or type(failure).__name__)
    assert raised.value.__cause__ is failure
    records = [
        record for record in caplog.records if record.name == "parqx.query.engine"
    ]
    assert len(records) == (1 if isinstance(failure, RuntimeError) else 0)
    if records:
        assert records[0].exc_info is not None
        assert records[0].exc_info[1] is failure


@pytest.mark.parametrize("failure", [None, "query", "read", "reader", "connection"])
def test_native_resources_close_in_order_even_after_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    closed: list[str] = []
    directories: list[Path] = []
    original_close = duckdb.DuckDBPyConnection.close
    batch = pa.RecordBatch.from_arrays([pa.array([42])], names=["answer"])

    class Reader:
        schema = batch.schema
        exhausted = False

        def read_next_batch(self) -> pa.RecordBatch:
            assert directories[0].exists()
            if failure == "read":
                raise OSError("read failed")
            if self.exhausted:
                raise StopIteration
            self.exhausted = True
            return batch

        def close(self) -> None:
            closed.append("reader")
            if failure == "reader":
                raise OSError("reader failed")

    def close(connection: duckdb.DuckDBPyConnection) -> None:
        original_close(connection)
        closed.append("connection")
        if failure == "connection":
            raise OSError("connection failed")

    def temporary(*, prefix: str) -> TemporaryDirectory[str]:
        directory = TemporaryDirectory(prefix=prefix, dir=tmp_path)
        directories.append(Path(directory.name))
        return directory

    monkeypatch.setattr(engine, "TemporaryDirectory", temporary)
    monkeypatch.setattr(duckdb.DuckDBPyConnection, "close", close)
    with patch.object(
        duckdb.DuckDBPyRelation, "to_arrow_reader", return_value=Reader()
    ):
        if failure:
            with pytest.raises(QueryExecutionError):
                execute_query(
                    (),
                    "SELECT missing" if failure == "query" else "SELECT 42",
                    config=ParqxConfig(),
                )
        else:
            result = execute_query((), "SELECT 42", config=ParqxConfig())
            assert result.table.column(0)[0].as_py() == 42
    assert closed == (
        ["connection"] if failure == "query" else ["reader", "connection"]
    )
    assert len(directories) == 1
    assert not directories[0].exists()


@pytest.mark.parametrize("invalid_file", [False, True])
def test_source_warnings_survive_success_error_and_recovery(
    small_parquet: Path, tmp_path: Path, invalid_file: bool
) -> None:
    unavailable = tmp_path / "unavailable.parquet"
    if invalid_file:
        unavailable.write_text("Not a Parquet file")
    inputs = sources(small_parquet, unavailable)
    earlier = (SourceIssue(inputs[1], "Still loading"),)
    result = execute_query(
        inputs, "SELECT count(*) FROM smoke", config=ParqxConfig(), unavailable=earlier
    )
    assert result.table.column(0)[0].as_py() == 5
    assert len(result.issues) == 2
    assert result.issues[0] == earlier[0]
    assert result.issues[1].source == inputs[1]
    with pytest.raises(QueryExecutionError, match="unavailable") as raised:
        execute_query(
            inputs,
            "SELECT * FROM unavailable",
            config=ParqxConfig(),
            unavailable=earlier,
        )
    assert isinstance(raised.value.__cause__, duckdb.CatalogException)
    assert len(raised.value.issues) == 2
    assert raised.value.issues[0] == earlier[0]
    unavailable.write_bytes(small_parquet.read_bytes())
    recovered = execute_query(
        inputs, "SELECT count(*) FROM unavailable", config=ParqxConfig()
    )
    assert recovered.table.column(0)[0].as_py() == 5
    assert not recovered.issues


def test_join_and_three_source_cte(small_parquet: Path, tmp_path: Path) -> None:
    orders, products = tmp_path / "orders.parquet", tmp_path / "a 'quoted' file.parquet"
    pq.write_table(
        pa.table(
            {"user_id": [1, 3, 1], "product_id": [8, 9, 9], "quantity": [2, 1, 3]}
        ),
        orders,
    )
    pq.write_table(pa.table({"id": [8, 9], "price": [5, 7]}), products)
    inputs = sources(small_parquet, orders, products)
    result = execute_query(
        inputs,
        "WITH totals AS (SELECT s.id, sum(o.quantity * p.price) AS cost FROM smoke s "
        "JOIN orders o ON s.id = o.user_id "
        f"JOIN {inputs[2].quoted_name} p ON p.id = o.product_id GROUP BY s.id) "
        "SELECT * FROM totals ORDER BY id",
        config=ParqxConfig(),
    )
    assert {
        name: [value.as_py() for value in result.table.column(name)]
        for name in result.table.column_names
    } == {"id": [1, 3], "cost": [31, 7]}
    assert result.issues == ()


def test_preview_limit_preserves_full_multi_source_input(
    small_parquet: Path, tmp_path: Path
) -> None:
    extra = tmp_path / "extra.parquet"
    pq.write_table(pa.table({"value": [10, 20]}), extra)
    result = execute_query(
        sources(small_parquet, extra),
        "WITH all_values AS (SELECT id AS value FROM smoke UNION ALL SELECT value FROM extra) "
        "SELECT count(*) AS n, sum(value) AS total FROM all_values",
        config=ParqxConfig(preview_rows=1),
    )
    assert {
        name: [value.as_py() for value in result.table.column(name)]
        for name in result.table.column_names
    } == {"n": [7], "total": [45]}
    assert not result.truncation_reason is not None


def test_no_implicit_data_alias_but_actual_data_file_is_available(
    small_parquet: Path, tmp_path: Path
) -> None:
    with pytest.raises(QueryExecutionError, match="data"):
        execute_query(
            sources(small_parquet), "SELECT * FROM data", config=ParqxConfig()
        )
    path = tmp_path / "data.parquet"
    path.write_bytes(small_parquet.read_bytes())
    result = execute_query(
        sources(path), "SELECT count(*) FROM data", config=ParqxConfig()
    )
    assert result.table.column(0)[0].as_py() == 5


@pytest.mark.parametrize("name", ['销售 many.dots-"items"', "select"])
def test_sql_identifiers_are_registered_without_rewriting(
    small_parquet: Path, name: str
) -> None:
    source = replace(sources(small_parquet)[0], table_name=name)
    result = execute_query(
        (source,), f"SELECT count(*) FROM {source.quoted_name}", config=ParqxConfig()
    )
    assert result.table.column(0)[0].as_py() == 5


def test_python_variables_are_not_implicit_query_sources() -> None:
    replacement_input = pa.table({"value": [42]})
    assert replacement_input.num_rows == 1
    with pytest.raises(QueryExecutionError, match="replacement_input"):
        execute_query((), "SELECT * FROM replacement_input", config=ParqxConfig())


@pytest.mark.parametrize(
    ("operation", "failure"),
    [
        ("read_parquet", duckdb.OutOfMemoryException("memory budget")),
        ("read_parquet", duckdb.InterruptException("interrupted")),
        ("create_view", duckdb.InvalidInputException("invalid view")),
    ],
)
def test_fatal_registration_errors_propagate(
    small_parquet: Path, operation: str, failure: duckdb.Error
) -> None:
    owner = (
        duckdb.DuckDBPyConnection
        if operation == "read_parquet"
        else duckdb.DuckDBPyRelation
    )
    with (
        patch.object(owner, operation, side_effect=failure),
        pytest.raises(QueryExecutionError) as raised,
    ):
        execute_query(sources(small_parquet), "SELECT 42", config=ParqxConfig())
    assert raised.value.__cause__ is failure
    assert raised.value.issues == ()


def test_registration_does_not_replace_a_prior_view(small_parquet: Path) -> None:
    source = sources(small_parquet)[0]
    with pytest.raises(QueryExecutionError, match="already exists"):
        execute_query((source, source), "SELECT 42", config=ParqxConfig())
