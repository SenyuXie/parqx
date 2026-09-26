from pathlib import Path
from threading import Event
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from parqx.data.parquet import ParquetSource, ReadCancelledError
from parqx.data.view import DataPage, TableData


def test_metadata_open_and_cross_group_windows(tmp_path: Path) -> None:
    path = tmp_path / "groups.parquet"
    pq.write_table(pa.table({"n": range(100)}), path, row_group_size=17)
    with patch.object(
        pq.ParquetFile, "iter_batches", side_effect=AssertionError("data")
    ):
        source = ParquetSource(path)
        assert source.row_count == 100
    for start, stop in [(0, 2), (15, 22), (51, 80), (99, 101), (100, 100)]:
        page = source.read_window(start, stop, Event())
        assert [v.as_py() for v in page.table.column(0)] == list(
            range(start, min(stop, 100))
        )


def test_wide_values_are_split_by_bytes(tmp_path: Path) -> None:
    path = tmp_path / "wide.parquet"
    pq.write_table(pa.table({"text": ["x" * 100] * 100}), path)
    source = ParquetSource(path, page_bytes=250)
    page = source.read_window(0, 100, Event())
    assert page.table.num_rows == 2
    assert page.table.nbytes <= 250
    next_page = source.read_window(page.stop, 100, Event())
    assert next_page.start == 2
    assert next_page.table.num_rows == 2


def test_cancel_and_file_change(small_parquet: Path) -> None:
    source = ParquetSource(small_parquet)
    cancelled = Event()
    cancelled.set()
    with pytest.raises(ReadCancelledError):
        source.read_window(0, 5, cancelled)
    small_parquet.write_bytes(b"changed")
    with pytest.raises(OSError, match="source file changed"):
        source.read_window(0, 5, Event())


def test_cache_miss_null_and_eviction_are_distinct() -> None:
    table = pa.table({"n": [1, None]})
    data = TableData(table.schema, 100, 100, cache_bytes=table.nbytes)
    assert data.peek(0, 0) is None
    data.add_page(DataPage(0, table))
    scalar = data.peek(1, 0)
    assert scalar is not None
    assert not scalar.is_valid
    data.add_page(DataPage(50, table))
    assert data.peek(0, 0) is None
    assert data.peek(50, 0) is not None
    assert data.cache_bytes <= data.cache_budget


def test_original_timestamp_precision_survives_windows(tmp_path: Path) -> None:
    path = tmp_path / "timestamp.parquet"
    original = pa.array([123456789, 987654321], type=pa.timestamp("ns", tz="UTC"))
    pq.write_table(pa.table({"ts": original}), path)
    source = ParquetSource(path)
    page = source.read_window(0, 2, Event())
    assert page.table.column(0)[0] == original[0]
    assert page.table.column(0)[1] == original[1]


def test_empty_file_keeps_schema(tmp_path: Path) -> None:
    path = tmp_path / "empty.parquet"
    pq.write_table(pa.table({"x": pa.array([], type=pa.int64())}), path)
    source = ParquetSource(path)
    assert source.row_count == 0
    assert source.read_window(0, 10, Event()).table.column_names == ["x"]
