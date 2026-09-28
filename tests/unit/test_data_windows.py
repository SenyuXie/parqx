from pathlib import Path
from threading import Event
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from parqx.data.parquet import ParquetSource
from parqx.data.result_store import ResultStore
from parqx.data.view import DataPage, ReadCancelledError, TableData


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


@pytest.mark.parametrize("backend", ["parquet", "result"])
@pytest.mark.parametrize(
    ("start", "stop", "max_rows", "max_bytes", "expected"),
    [
        (-5, 2, 4, 24, [0, 1]),
        (2, 9, 4, 27, [2, 3, 4]),  # Byte limit across a batch boundary.
        (2, 9, 4, 1024, [2, 3, 4, 5]),  # Row limit across a batch boundary.
        (2, 9, 4, 1, [2]),  # One oversized row must still make progress.
        (5, 4, 4, 1024, []),
        (50, 60, 4, 1024, []),
        (6, 20, 256, 1024, [6, 7, 8]),
    ],
)
def test_window_budgets_match_across_backends(
    tmp_path: Path,
    backend: str,
    start: int,
    stop: int,
    max_rows: int,
    max_bytes: int,
    expected: list[int],
) -> None:
    table = pa.table({"n": range(9)})
    source: ParquetSource | ResultStore
    if backend == "parquet":
        path = tmp_path / "bounded.parquet"
        pq.write_table(table, path, row_group_size=3)
        source = ParquetSource(path, page_rows=max_rows, page_bytes=max_bytes)
    else:
        source = ResultStore(table.schema, page_rows=max_rows, page_bytes=max_bytes)
        for batch in table.to_batches(max_chunksize=3):
            source.append(batch)
    try:
        page = source.read_window(start, stop, Event())
        assert page.start == max(0, min(start, 9))
        assert page.table.schema == table.schema
        assert [value.as_py() for value in page.table.column(0)] == expected
        assert page.table.nbytes <= max_bytes or page.table.num_rows == 1
    finally:
        if isinstance(source, ResultStore):
            source.close()
