from collections.abc import Iterator
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from parqx.data import parquet
from parqx.data.batch import bounded_prefix
from parqx.data.parquet import ParquetSource, ReadCancelledError
from parqx.data.view import TableData


def test_metadata_open_and_cross_group_windows(tmp_path: Path) -> None:
    path = tmp_path / "groups.parquet"
    timestamps = pa.array(
        [None if index == 16 else 123456789 + index for index in range(100)],
        type=pa.timestamp("ns", tz="UTC"),
    )
    pq.write_table(
        pa.table({"n": range(100), "ts": timestamps}), path, row_group_size=17
    )
    with (
        patch("pyarrow.parquet.read_table", side_effect=AssertionError("full read")),
        patch.object(pq.ParquetFile, "read", side_effect=AssertionError("full read")),
        patch.object(
            pq.ParquetFile, "read_row_group", side_effect=AssertionError("full group")
        ),
    ):
        with patch.object(
            pq.ParquetFile, "iter_batches", side_effect=AssertionError("column data")
        ):
            source = ParquetSource(path)
            assert source.row_count == 100
        for start, stop in [(-2, 2), (15, 22), (51, 80), (99, 101), (100, 100)]:
            page = source.read_window(start, stop, Event())
            assert page.table.schema.equals(source.schema)
            assert list(page.table.column("ts")) == list(
                timestamps.slice(max(start, 0), min(stop, 100) - max(start, 0))
            )
            assert page.start == max(start, 0)
            assert [v.as_py() for v in page.table.column(0)] == list(
                range(max(start, 0), min(stop, 100))
            )


def test_window_skips_unrelated_row_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "groups.parquet"
    pq.write_table(pa.table({"n": range(100)}), path, row_group_size=17)
    source = ParquetSource(path, page_rows=4)
    visited: list[int] = []
    original = pq.ParquetFile.iter_batches

    def read_batches(
        file: pq.ParquetFile, *, batch_size: int, row_groups: list[int]
    ) -> Iterator[pa.RecordBatch]:
        visited.extend(row_groups)
        return original(file, batch_size=batch_size, row_groups=row_groups)

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", read_batches)
    page = source.read_window(35, 80, Event())
    assert visited == [2]
    assert page.stop == 39
    assert [v.as_py() for v in page.table.column(0)] == [35, 36, 37, 38]


@pytest.mark.parametrize("cancelled", [False, True])
def test_empty_or_cancelled_request_does_no_file_io(
    small_parquet: Path, cancelled: bool
) -> None:
    source = ParquetSource(small_parquet)
    signal = Event()
    if cancelled:
        signal.set()
    with (
        patch.object(Path, "stat", side_effect=AssertionError("stat")),
        patch("pyarrow.parquet.ParquetFile", side_effect=AssertionError("open")),
    ):
        if cancelled:
            with pytest.raises(ReadCancelledError):
                source.read_window(0, 5, signal)
        else:
            page = source.read_window(3, 2, signal)
            assert page.start == page.stop == 3
            assert page.table.column_names == ["id", "name", "score"]


def test_cancellation_after_decoding_discards_the_batch(
    small_parquet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = ParquetSource(small_parquet)
    cancelled = Event()
    original = pq.ParquetFile.iter_batches

    def cancel_during_read(
        file: pq.ParquetFile, *, batch_size: int, row_groups: list[int]
    ) -> Iterator[pa.RecordBatch]:
        reader = original(file, batch_size=batch_size, row_groups=row_groups)
        batch = next(reader)
        cancelled.set()
        yield batch
        pytest.fail("A cancelled read requested another batch")

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", cancel_during_read)
    with pytest.raises(ReadCancelledError):
        source.read_window(0, 5, cancelled)


def test_cancellation_is_checked_before_requesting_another_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "groups.parquet"
    pq.write_table(pa.table({"n": range(5)}), path, row_group_size=1)
    source = ParquetSource(path)
    cancelled = Event()
    original = pq.ParquetFile.iter_batches

    def read_one_batch(
        file: pq.ParquetFile, *, batch_size: int, row_groups: list[int]
    ) -> Iterator[pa.RecordBatch]:
        yield next(original(file, batch_size=batch_size, row_groups=row_groups))
        pytest.fail("Cancellation must be checked before advancing the iterator")

    def cancel_after_prefix(
        batch: pa.RecordBatch, budget: int, *, allow_one: bool
    ) -> int:
        cancelled.set()
        return bounded_prefix(batch, budget, allow_one=allow_one)

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", read_one_batch)
    monkeypatch.setattr(parquet, "bounded_prefix", cancel_after_prefix)
    with pytest.raises(ReadCancelledError):
        source.read_window(0, 5, cancelled)


def test_source_file_changes_are_rejected(small_parquet: Path) -> None:
    source = ParquetSource(small_parquet)
    small_parquet.write_bytes(b"changed")
    with pytest.raises(OSError, match=r"source file changed.*Reopen"):
        source.read_window(0, 5, Event())


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


def test_single_oversized_value_can_be_read(tmp_path: Path) -> None:
    path = tmp_path / "oversized.parquet"
    value = "x" * 1000
    pq.write_table(pa.table({"text": [value, "small"]}), path)
    source = ParquetSource(path, page_bytes=16)
    page = source.read_window(0, 2, Event())
    assert page.table.num_rows == 1
    assert page.table.column(0)[0].as_py() == value
    assert source.read_window(1, 2, Event()).table.column(0)[0].as_py() == "small"


def test_window_does_not_retain_discarded_decode_buffers(tmp_path: Path) -> None:
    path = tmp_path / "buffers.parquet"
    pq.write_table(pa.table({"text": ["small", "x" * 8192]}), path)
    source = ParquetSource(path, page_bytes=16)
    page = source.read_window(0, 2, Event())
    assert page.table.num_rows == 1
    assert page.table.column(0)[0].as_py() == "small"
    assert page.table.get_total_buffer_size() < 1024


@pytest.mark.parametrize(
    ("kind", "prefix_length"),
    # Cover each container and both inline/external view storage layouts.
    [("string", 5), ("binary", 1000), ("list", 5), ("nested", 1000)],
)
def test_view_windows_preserve_schema_and_compact_buffers(
    tmp_path: Path, kind: str, prefix_length: int
) -> None:
    short = "x" * prefix_length
    tail = "y" * 8192
    data_type: pa.DataType
    values: list[object]
    if kind == "nested":
        data_type = pa.struct(
            [
                pa.field("text", pa.string_view(), nullable=False, metadata={"a": "b"}),
                pa.field("time", pa.timestamp("ns", tz="UTC")),
            ]
        )
        values = [{"text": short, "time": 123456789}, {"text": tail, "time": 987654321}]
    elif kind == "list":
        data_type = pa.list_(pa.string_view())
        values = [[short], [tail]]
    elif kind == "binary":
        data_type = pa.binary_view()
        values = [short.encode(), tail.encode()]
    else:
        data_type = pa.string_view()
        values = [short, tail]
    original = pa.array(values, type=data_type)
    schema = pa.schema(
        [pa.field("value", data_type, nullable=False, metadata={"field": "kept"})],
        metadata={"table": "kept"},
    )
    path = tmp_path / "views.parquet"
    pq.write_table(pa.table({"value": original}, schema=schema), path)
    source = ParquetSource(path, page_rows=2, page_bytes=2048)
    page = source.read_window(0, 2, Event())
    assert page.table.num_rows == 1
    assert page.table.schema.equals(source.schema, check_metadata=True)
    assert page.table.column(0)[0] == original[0]
    assert page.table.nbytes <= 2048
    assert page.table.get_total_buffer_size() < prefix_length + 512

    data = TableData(source.schema, source.row_count)
    data.add_page(page)
    assert data.schema.equals(source.schema, check_metadata=True)
    assert data.peek(0, 0) == original[0]
    assert data.cache_bytes == page.table.nbytes


def test_empty_file_keeps_schema(tmp_path: Path) -> None:
    path = tmp_path / "empty.parquet"
    pq.write_table(pa.table({"x": pa.array([], type=pa.int64())}), path)
    source = ParquetSource(path)
    assert source.row_count == 0
    page = source.read_window(0, 10, Event())
    assert page.table.column_names == ["x"]
    assert page.start == page.stop == 0


def test_dictionary_windows_drop_unused_categories_and_keep_cached_pages(
    tmp_path: Path,
) -> None:
    # Unused categories exceed the page budget; the retained dictionary fits.
    values = pa.array([str(index).zfill(100) for index in range(4096)])
    column = pa.DictionaryArray.from_arrays(
        pa.array([0] * 512, type=pa.int64()), values
    )
    path = tmp_path / "categories.parquet"
    pq.write_table(pa.table({"category": column}), path)
    source = ParquetSource(path, page_bytes=4096)
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


def test_nested_dictionary_parquet_keeps_only_referenced_categories(
    tmp_path: Path,
) -> None:
    dictionary = pa.DictionaryArray.from_arrays(
        pa.array([1, 0, 1], type=pa.int8()), pa.array(["x" * 8192, "ok"])
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
