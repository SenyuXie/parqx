from pathlib import Path
from unittest.mock import patch

import duckdb
import pyarrow as pa
import pytest

from parqx.catalog import SourceCatalog, SourceSpec
from parqx.query.engine import QueryControl, QueryLimits, QuerySession


@pytest.fixture
def query_sources(small_parquet: Path) -> tuple[SourceSpec, ...]:
    return tuple(entry.spec for entry in SourceCatalog([small_parquet]).entries)


@pytest.mark.parametrize(
    ("row_limit", "batch_rows", "expected_rows", "truncated"),
    [(3, 2, 3, True), (4, 2, 4, True), (5, 2, 5, False)],
)
def test_row_budget_boundaries(
    query_sources: tuple[SourceSpec, ...],
    row_limit: int,
    batch_rows: int,
    expected_rows: int,
    truncated: bool,
) -> None:
    with QuerySession(
        query_sources,
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


def test_byte_budget_and_exact_result_boundary(
    query_sources: tuple[SourceSpec, ...],
) -> None:
    sql = "SELECT id FROM smoke ORDER BY id"
    with QuerySession(query_sources, sql, QueryControl()) as session:
        complete = session.preview()
    for budget, truncated in [(16, True), (complete.table.nbytes, False)]:
        with QuerySession(
            query_sources, sql, QueryControl(), QueryLimits(preview_bytes=budget)
        ) as session:
            result = session.preview()
        assert result.table.num_rows >= 1
        assert result.table.nbytes <= budget
        assert result.truncated is truncated
        assert result.reason == ("byte budget" if truncated else None)


def test_one_oversized_value_is_still_available(
    query_sources: tuple[SourceSpec, ...],
) -> None:
    value = "x" * 4096
    with QuerySession(
        query_sources,
        "SELECT repeat('x', 4096) AS value FROM smoke",
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
    query_sources: tuple[SourceSpec, ...],
) -> None:
    with QuerySession(
        query_sources,
        "SELECT CASE WHEN id = 1 THEN 'ok' ELSE repeat('x', 8192) END AS value "
        "FROM smoke ORDER BY id",
        QueryControl(),
        QueryLimits(preview_rows=1, batch_rows=5),
    ) as session:
        result = session.preview()
    assert result.table.column(0)[0].as_py() == "ok"
    assert result.truncated
    assert result.table.get_total_buffer_size() < 1024


def test_empty_result_keeps_its_schema(query_sources: tuple[SourceSpec, ...]) -> None:
    with QuerySession(
        query_sources, "SELECT id FROM smoke WHERE false", QueryControl()
    ) as session:
        result = session.preview()
    assert result.table.num_rows == 0
    assert result.table.column_names == ["id"]
    assert result.table.column(0).type == pa.int64()
    assert not result.truncated
    assert result.reason is None


@pytest.mark.parametrize("row_limit", [2, 3])
def test_query_dictionary_preview_uses_compacted_budget(row_limit: int) -> None:
    column = pa.DictionaryArray.from_arrays(
        pa.array([0, 0, 0], type=pa.int8()), pa.array(["ok", "x" * 8192])
    )
    batch = pa.RecordBatch.from_arrays([column], names=["category"])
    reader = pa.RecordBatchReader.from_batches(batch.schema, [batch])
    with (
        patch.object(duckdb.DuckDBPyRelation, "to_arrow_reader", return_value=reader),
        QuerySession(
            (),
            "SELECT 1",
            QueryControl(),
            QueryLimits(preview_rows=row_limit, preview_bytes=10),
        ) as session,
    ):
        result = session.preview()
    assert result.table.num_rows == row_limit
    assert result.truncated is (row_limit == 2)
    assert result.table.nbytes <= 10
    assert result.table.get_total_buffer_size() < 128
