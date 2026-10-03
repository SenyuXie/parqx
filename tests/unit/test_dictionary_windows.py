from pathlib import Path
from threading import Event
from unittest.mock import patch

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from parqx.data.batch import bounded_prefix, compact_batch
from parqx.data.parquet import ParquetSource
from parqx.data.view import TableData
from parqx.query.engine import QueryControl, QueryLimits, QuerySession


def test_large_dictionary_windows_keep_rows_and_cached_pages(tmp_path: Path) -> None:
    values = pa.array([str(index).zfill(100) for index in range(350_000)])
    column = pa.DictionaryArray.from_arrays(
        pa.array([0] * 512, type=pa.int64()), values
    )
    path = tmp_path / "categories.parquet"
    pq.write_table(pa.table({"category": column}), path)
    source = ParquetSource(path)
    data = TableData(source.schema, source.row_count)
    for start in (0, 256):
        page = source.read_window(start, start + 256, Event())
        assert page.table.num_rows == 256
        assert page.table.schema.equals(source.schema, check_metadata=True)
        assert page.table.get_total_buffer_size() < 4096
        data.add_page(page)
    assert data.cache_bytes < 8192
    first = data.peek(0, 0)
    second = data.peek(256, 0)
    assert first is not None
    assert first.as_py() == column[0].as_py()
    assert second is not None
    assert second.as_py() == column[256].as_py()


def test_dictionary_prefix_budget_excludes_later_references() -> None:
    column = pa.DictionaryArray.from_arrays(
        pa.array([0, 0, 0, 1], type=pa.int8()), pa.array(["ok", "x" * 1_000_000])
    )
    batch = pa.RecordBatch.from_arrays([column], names=["category"])
    assert bounded_prefix(batch, 10, allow_one=False) == 3
    kept = compact_batch(batch.slice(0, 3))
    assert kept.nbytes == 10
    assert kept.column(0)[0].as_py() == column[0].as_py()
    assert pa.Table.from_batches([kept]).get_total_buffer_size() < 128


@pytest.mark.parametrize("ordered", [False, True])
def test_dictionary_compaction_preserves_order_nulls_and_nested_precision(
    ordered: bool,
) -> None:
    value_type = pa.list_(pa.timestamp("ns", tz="UTC"))
    column = pa.DictionaryArray.from_arrays(
        pa.array([3, None, 1, 3, 2], type=pa.int8()),
        pa.array([[123456789], [987654321], None, [100000001]], type=value_type),
        ordered=ordered,
    )
    schema = pa.schema(
        [pa.field("category", column.type, metadata={"field": "kept"})],
        metadata={"table": "kept"},
    )
    batch = pa.RecordBatch.from_arrays([column], schema=schema)
    compact = compact_batch(batch)
    assert compact.schema.equals(schema, check_metadata=True)
    kept = compact.column(0)
    assert isinstance(kept, pa.DictionaryArray)
    assert [index.as_py() for index in kept.indices] == [2, None, 0, 2, 1]
    assert len(kept.dictionary) == 3
    assert list(kept.dictionary_decode()) == list(column.dictionary_decode())
    assert bounded_prefix(batch, compact.nbytes, allow_one=False) == batch.num_rows
    assert bounded_prefix(batch, compact.nbytes - 1, allow_one=False) < batch.num_rows


def test_all_null_dictionary_drops_every_value() -> None:
    column = pa.DictionaryArray.from_arrays(
        pa.array([None] * 16, type=pa.int8()), pa.array(["x" * 1_000_000])
    )
    batch = pa.RecordBatch.from_arrays([column], names=["category"])
    compact = compact_batch(batch)
    kept = compact.column(0)
    assert isinstance(kept, pa.DictionaryArray)
    assert len(kept.dictionary) == 0
    assert kept.null_count == 16
    assert compact.nbytes == 18
    assert bounded_prefix(batch, 18, allow_one=False) == 16
    assert pa.Table.from_batches([compact]).get_total_buffer_size() < 128


def test_nested_dictionary_parquet_keeps_only_referenced_categories(
    tmp_path: Path,
) -> None:
    dictionary = pa.DictionaryArray.from_arrays(
        pa.array([1, 0, 1], type=pa.int8()), pa.array(["x" * 1_000_000, "ok"])
    )
    nested = pa.ListArray.from_arrays(pa.array([0, 1, 2, 3]), dictionary)
    path = tmp_path / "nested.parquet"
    pq.write_table(pa.table({"values": nested}), path)
    source = ParquetSource(path, page_bytes=32)
    first = source.read_window(0, 3, Event())
    assert first.table.num_rows == 1
    assert first.table.column(0)[0].as_py() == nested[0].as_py()
    assert first.table.get_total_buffer_size() < 128
    last = source.read_window(2, 3, Event())
    assert last.table.column(0)[0].as_py() == nested[2].as_py()
    assert last.table.get_total_buffer_size() < 128


@pytest.mark.parametrize("row_limit", [2, 3])
def test_query_dictionary_preview_uses_compacted_budget(row_limit: int) -> None:
    column = pa.DictionaryArray.from_arrays(
        pa.array([0, 0, 0], type=pa.int8()), pa.array(["ok", "x" * 1_000_000])
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


@pytest.mark.parametrize("kind", ["struct", "list", "large_list", "fixed_list", "map"])
def test_nested_dictionary_types_preserve_masks_and_fields(kind: str) -> None:
    category = pa.dictionary(pa.int8(), pa.string(), ordered=True)
    field = pa.field("category", category, metadata={"field": "kept"})
    data_type: pa.DataType
    values: list[object]
    if kind == "struct":
        data_type = pa.struct([field, pa.field("time", pa.timestamp("ns"))])
        values = [
            {"category": "ok", "time": 123456789},
            None,
            {"category": "x" * 1_000_000, "time": 987654321},
        ]
    elif kind == "map":
        data_type = pa.map_(pa.string(), field)
        values = [[("key", "ok")], None, [("key", "x" * 1_000_000)]]
    else:
        data_type = (
            pa.large_list(field)
            if kind == "large_list"
            else pa.list_(field, list_size=1 if kind == "fixed_list" else -1)
        )
        values = [["ok"], None, ["x" * 1_000_000]]
    array = pa.array(values, type=data_type)
    batch = pa.RecordBatch.from_arrays([array], names=["nested"])
    result = compact_batch(batch.slice(0, 2))
    assert result.schema.equals(batch.schema, check_metadata=True)
    assert not result.column(0)[1].is_valid
    assert result.nbytes < 128
    assert pa.Table.from_batches([result]).get_total_buffer_size() < 256
    # Compare the timestamp as an Arrow scalar so nanoseconds are never rounded.
    if kind == "struct":
        assert isinstance(array, pa.StructArray)
        kept = result.column(0)
        assert isinstance(kept, pa.StructArray)
        assert kept.field("time")[0] == array.field("time")[0]
    else:
        assert result.column(0)[0].as_py() == array[0].as_py()
    assert bounded_prefix(batch, result.nbytes, allow_one=False) == 2


@pytest.mark.parametrize("large_values_are_used", [False, True])
@pytest.mark.parametrize("kind", ["direct", "list", "struct"])
def test_dictionary_budget_measurement_has_bounded_allocations(
    large_values_are_used: bool, kind: str
) -> None:
    if large_values_are_used:
        values = pa.array(["x" * 16_000] * 256)
        indices = pa.array(range(256), type=pa.int64())
    else:
        values = pa.array([str(index).zfill(100) for index in range(350_000)])
        indices = pa.array([0] * 256, type=pa.int64())
    column = pa.DictionaryArray.from_arrays(indices, values)
    nested: pa.Array
    if kind == "list":
        nested = pa.ListArray.from_arrays(pa.array(range(257)), column)
    elif kind == "struct":
        nested = pa.Array.from_buffers(
            pa.struct([("category", column.type)]), 256, [None], children=[column]
        )
    else:
        nested = column
    batch = pa.RecordBatch.from_arrays([nested], names=["category"])
    parent = pa.default_memory_pool()
    pool = pa.proxy_memory_pool(parent)
    try:
        pa.set_memory_pool(pool)
        keep = bounded_prefix(batch, 4096, allow_one=True)
        peak = pool.max_memory()
    finally:
        pa.set_memory_pool(parent)
    assert keep == (1 if large_values_are_used else 256)
    assert pool.bytes_allocated() == 0
    assert peak is not None
    assert peak < 64 * 1024
