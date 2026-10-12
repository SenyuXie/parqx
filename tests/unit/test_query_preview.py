from pathlib import Path
from unittest.mock import patch

import duckdb
import pyarrow as pa
import pytest

from parqx.catalog import SourceCatalog, SourceSpec
from parqx.config import ParqxConfig
from parqx.query.engine import execute_query


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
    result = execute_query(
        query_sources,
        "SELECT id FROM smoke ORDER BY id",
        config=ParqxConfig(preview_rows=row_limit, query_batch_rows=batch_rows),
    )
    assert [value.as_py() for value in result.table.column(0)] == list(
        range(1, expected_rows + 1)
    )
    assert (result.truncation_reason is not None) is truncated
    assert result.truncation_reason == ("row limit" if truncated else None)


def test_byte_budget_and_exact_result_boundary(
    query_sources: tuple[SourceSpec, ...],
) -> None:
    sql = "SELECT id FROM smoke ORDER BY id"
    complete = execute_query(query_sources, sql, config=ParqxConfig())
    for budget, truncated in [(16, True), (complete.table.nbytes, False)]:
        result = execute_query(
            query_sources, sql, config=ParqxConfig(preview_bytes=budget)
        )
        assert result.table.num_rows >= 1
        assert result.table.nbytes <= budget
        assert (result.truncation_reason is not None) is truncated
        assert result.truncation_reason == ("byte budget" if truncated else None)


def test_one_oversized_value_is_still_available(
    query_sources: tuple[SourceSpec, ...],
) -> None:
    value = "x" * 4096
    result = execute_query(
        query_sources,
        "SELECT repeat('x', 4096) AS value FROM smoke",
        config=ParqxConfig(preview_bytes=16),
    )
    assert result.table.num_rows == 1
    assert result.table.column(0)[0].as_py() == value
    assert result.table.nbytes > 16
    assert result.truncation_reason is not None
    assert result.truncation_reason == "byte budget"


def test_truncated_prefix_does_not_retain_discarded_buffers(
    query_sources: tuple[SourceSpec, ...],
) -> None:
    result = execute_query(
        query_sources,
        "SELECT CASE WHEN id = 1 THEN 'ok' ELSE repeat('x', 8192) END AS value FROM smoke ORDER BY id",
        config=ParqxConfig(preview_rows=1, query_batch_rows=5),
    )
    assert result.table.column(0)[0].as_py() == "ok"
    assert result.truncation_reason is not None
    assert result.table.get_total_buffer_size() < 1024


def test_empty_result_keeps_its_schema(query_sources: tuple[SourceSpec, ...]) -> None:
    result = execute_query(
        query_sources, "SELECT id FROM smoke WHERE false", config=ParqxConfig()
    )
    assert result.table.num_rows == 0
    assert result.table.column_names == ["id"]
    assert result.table.column(0).type == pa.int64()
    assert result.truncation_reason is None


@pytest.mark.parametrize("row_limit", [2, 3])
def test_query_dictionary_preview_uses_compacted_budget(row_limit: int) -> None:
    column = pa.DictionaryArray.from_arrays(
        pa.array([0, 0, 0], type=pa.int8()), pa.array(["ok", "x" * 8192])
    )
    batch = pa.RecordBatch.from_arrays([column], names=["category"])
    reader = pa.RecordBatchReader.from_batches(batch.schema, [batch])
    with patch.object(duckdb.DuckDBPyRelation, "to_arrow_reader", return_value=reader):
        result = execute_query(
            (), "SELECT 1", config=ParqxConfig(preview_rows=row_limit, preview_bytes=10)
        )
    assert result.table.num_rows == row_limit
    assert (result.truncation_reason is not None) is (row_limit == 2)
    assert result.table.nbytes <= 10
    assert result.table.get_total_buffer_size() < 128
