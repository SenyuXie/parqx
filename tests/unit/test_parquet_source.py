"""DuckDB file metadata, ordered paging, budgets and cancellation."""

from datetime import UTC, datetime
from decimal import Decimal
from os import name as os_name
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from parqx.data.duckdb import QueryCancelledError, QueryControl
from parqx.data.parquet import ParquetSource
from parqx.data.view import TableData


def test_metadata_and_ordered_cross_group_windows(tmp_path: Path) -> None:
    path = tmp_path / "groups.parquet"
    table = pa.table({"n": range(100), "name": [f"row-{i}" for i in range(100)]})
    pq.write_table(table, path, row_group_size=17)
    with patch("pyarrow.parquet.ParquetFile", side_effect=AssertionError("Arrow read")):
        source = ParquetSource(path)
        assert source.row_count == 100
        for start, stop in [(-2, 2), (15, 22), (51, 80), (99, 101), (100, 100)]:
            page = source.read_window(start, stop, QueryControl())
            assert page.start == max(start, 0)
            assert page.table.schema.equals(source.schema)
            assert page.table.equals(
                table.slice(max(start, 0), min(stop, 100) - max(start, 0))
            )


def test_metadata_does_not_decode_column_data(tmp_path: Path) -> None:
    path = tmp_path / "corrupt-page.parquet"
    pq.write_table(
        pa.table({"n": range(1000)}), path, compression="NONE", use_dictionary=False
    )
    with duckdb.connect() as connection:
        metadata = connection.execute(
            "SELECT data_page_offset FROM parquet_metadata(?)", [str(path)]
        ).fetchone()
    assert metadata is not None
    with path.open("r+b") as file:
        file.seek(int(metadata[0]))
        file.write(b"\xff" * 32)
    source = ParquetSource(path)
    assert source.row_count == 1000
    assert source.schema.names == ["n"]
    with pytest.raises(duckdb.Error):
        source.read_window(0, 256, QueryControl())


def test_page_row_limit_is_independent_of_requested_window(tmp_path: Path) -> None:
    path = tmp_path / "prefetch.parquet"
    table = pa.table({"n": range(8192)})
    pq.write_table(table, path)
    source = ParquetSource(path)
    page = source.read_window(0, 8192, QueryControl())
    assert page.table.equals(table.slice(0, 4096))
    assert (
        ParquetSource(path, page_rows=4).read_window(35, 80, QueryControl()).stop == 39
    )


@pytest.mark.parametrize("cancelled", [False, True])
def test_empty_or_cancelled_request_creates_no_resources(
    small_parquet: Path, cancelled: bool
) -> None:
    source = ParquetSource(small_parquet)
    control = QueryControl()
    if cancelled:
        control.cancel()
    with (
        patch.object(Path, "stat", side_effect=AssertionError("stat")),
        patch("parqx.data.duckdb.duckdb.connect", side_effect=AssertionError("open")),
    ):
        if cancelled:
            with pytest.raises(QueryCancelledError):
                source.read_window(0, 5, control)
        else:
            page = source.read_window(3, 2, control)
            assert page.start == page.stop == 3
            assert page.table.schema.equals(source.schema)


def test_source_file_changes_are_rejected(small_parquet: Path) -> None:
    source = ParquetSource(small_parquet)
    small_parquet.write_bytes(b"changed")
    with pytest.raises(OSError, match=r"source file changed.*Reopen"):
        source.read_window(0, 5, QueryControl())


def test_window_byte_budget_applies_across_batches(tmp_path: Path) -> None:
    path = tmp_path / "wide.parquet"
    table = pa.table({"text": ["x" * 100] * 500})
    pq.write_table(table, path)
    budget = table.slice(0, 300).nbytes
    source = ParquetSource(path, page_bytes=budget)
    page = source.read_window(0, 4096, QueryControl())
    assert 256 < page.stop < 500
    assert page.table.equals(table.slice(0, page.stop))
    assert page.table.nbytes <= budget
    assert source.read_window(page.stop, 4096, QueryControl()).table.equals(
        table.slice(page.stop)
    )


def test_single_oversized_value_can_be_read(tmp_path: Path) -> None:
    path = tmp_path / "oversized.parquet"
    value = "x" * 1000
    pq.write_table(pa.table({"text": [value, "small"]}), path)
    source = ParquetSource(path, page_bytes=16)
    page = source.read_window(0, 2, QueryControl())
    assert page.table.num_rows == 1
    assert page.table.column(0)[0].as_py() == value
    assert (
        source.read_window(1, 2, QueryControl()).table.column(0)[0].as_py() == "small"
    )


def test_window_does_not_retain_discarded_buffers(tmp_path: Path) -> None:
    path = tmp_path / "buffers.parquet"
    pq.write_table(pa.table({"text": ["small", "x" * 8192]}), path)
    source = ParquetSource(path, page_bytes=16)
    page = source.read_window(0, 2, QueryControl())
    assert page.table.num_rows == 1
    assert page.table.column(0)[0].as_py() == "small"
    assert page.table.get_total_buffer_size() < 1024
    cache = TableData(source.schema, source.row_count)
    cache.add_page(page)
    assert cache.cache_bytes <= 16


def test_empty_file_keeps_schema(tmp_path: Path) -> None:
    path = tmp_path / "empty.parquet"
    pq.write_table(pa.table({"x": pa.array([], type=pa.int64())}), path)
    source = ParquetSource(path)
    assert source.row_count == 0
    page = source.read_window(0, 10, QueryControl())
    assert page.table.column_names == ["x"]
    assert page.start == page.stop == 0


@pytest.mark.parametrize(
    ("literal", "neighbor"),
    [("left[1]", "left1"), ("parts*", "parts-extra"), ("问?号", "问1号")],
)
def test_source_path_is_literal_including_parent_directory(
    tmp_path: Path, literal: str, neighbor: str
) -> None:
    if os_name == "nt" and any(char in literal for char in "*?"):
        pytest.skip("Windows filenames cannot contain * or ?")
    directory = tmp_path / "batch[1]"
    directory.mkdir()
    path = directory / f"{literal}.parquet"
    pq.write_table(pa.table({"value": ["opened"]}), path)
    pq.write_table(pa.table({"value": ["wrong"]}), directory / f"{neighbor}.parquet")
    source = ParquetSource(path)
    assert source.row_count == 1
    assert (
        source.read_window(0, 1, QueryControl()).table.column(0)[0].as_py() == "opened"
    )
    path.unlink()
    with pytest.raises(FileNotFoundError):
        source.read_window(0, 1, QueryControl())


def test_file_row_number_and_partition_named_directories_are_plain_data(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "year=2026"
    directory.mkdir()
    path = directory / "names.parquet"
    table = pa.table({"file_row_number": [900, 100, 500], "year": [1, 2, 3]})
    pq.write_table(table, path, row_group_size=1)
    source = ParquetSource(path)
    assert source.schema.names == table.schema.names
    assert source.read_window(1, 3, QueryControl()).table.equals(table.slice(1))


def test_metadata_and_pages_share_duckdb_type_semantics(tmp_path: Path) -> None:
    path = tmp_path / "types.parquet"
    pq.write_table(
        pa.table(
            {
                "time": pa.array([123456789], type=pa.timestamp("ns", tz="UTC")),
                "category": pa.DictionaryArray.from_arrays(
                    pa.array([0], type=pa.int8()), pa.array(["shown"])
                ),
                "A": [1],
                "a": [2],
            }
        ),
        path,
    )
    source = ParquetSource(path)
    page = source.read_window(0, 1, QueryControl())
    assert page.table.schema.equals(source.schema)
    assert source.schema.names == ["time", "category", "A", "a_1"]
    assert source.schema.field("category").type == pa.string()
    assert page.table.column("time")[0].as_py() == datetime(
        1970, 1, 1, 0, 0, 0, 123456, tzinfo=UTC
    )
    assert page.table.column("category")[0].as_py() == "shown"


@pytest.mark.parametrize("precision", [39, 50, 76])
@pytest.mark.parametrize("nested", [False, True])
def test_unsupported_decimal_precision_is_rejected_before_reading_values(
    tmp_path: Path, precision: int, nested: bool
) -> None:
    path = tmp_path / "decimal.parquet"
    decimal_type = pa.decimal256(precision, 2)
    data_type = (
        pa.struct([pa.field("amount", decimal_type)]) if nested else decimal_type
    )
    value = {"amount": Decimal("123456.78")} if nested else Decimal("123456.78")
    pq.write_table(pa.table({"value": pa.array([value], type=data_type)}), path)
    with pytest.raises(ValueError, match=r"decimal precision .*at most 38"):
        ParquetSource(path)


def test_supported_decimal_precision_preserves_values(tmp_path: Path) -> None:
    path = tmp_path / "decimal38.parquet"
    value = Decimal("123456789012345678901234567890123456.78")
    pq.write_table(
        pa.table({"amount": pa.array([value], type=pa.decimal256(38, 2))}), path
    )
    source = ParquetSource(path)
    assert source.read_window(0, 1, QueryControl()).table.column(0)[0].as_py() == value


def test_cancellation_interrupts_a_native_window_and_next_read_succeeds(
    small_parquet: Path,
) -> None:
    source = ParquetSource(small_parquet)
    control = QueryControl()
    entered, release, finished = Event(), Event(), Event()
    errors: list[BaseException] = []
    original_reader = duckdb.DuckDBPyRelation.to_arrow_reader

    def gated_reader(
        relation: duckdb.DuckDBPyRelation, batch_rows: int
    ) -> pa.RecordBatchReader:
        entered.set()
        assert release.wait(timeout=10)
        expensive = relation.query(
            "source_rows",
            "SELECT sum(sin(i)) AS value FROM source_rows, range(1000000000) t(i)",
        )
        return original_reader(expensive, batch_rows)

    def read() -> None:
        try:
            source.read_window(0, 5, control)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    with patch.object(duckdb.DuckDBPyRelation, "to_arrow_reader", gated_reader):
        thread = Thread(target=read, daemon=True)
        thread.start()
        try:
            assert entered.wait(timeout=10)
            control.cancel()
            release.set()
            assert finished.wait(timeout=5), "Native browsing query did not stop"
            assert len(errors) == 1
            assert isinstance(
                errors[0], (QueryCancelledError, duckdb.InterruptException)
            )
        finally:
            release.set()
            control.cancel()
            thread.join(timeout=5)
            assert not thread.is_alive()
    assert source.read_window(0, 1, QueryControl()).table.num_rows == 1


def test_failed_read_detaches_before_closing_reader_and_connection(
    small_parquet: Path,
) -> None:
    source = ParquetSource(small_parquet)
    control = QueryControl()
    closed: list[str] = []
    original_detach = control.detach
    original_close = duckdb.DuckDBPyConnection.close

    def detach() -> None:
        original_detach()
        closed.append("control")

    def close_connection(connection: duckdb.DuckDBPyConnection) -> None:
        original_close(connection)
        closed.append("connection")

    class Reader:
        schema = source.schema

        def read_next_batch(self) -> pa.RecordBatch:
            raise RuntimeError("read failed")

        def close(self) -> None:
            closed.append("reader")

    with (
        patch.object(control, "detach", detach),
        patch.object(duckdb.DuckDBPyConnection, "close", close_connection),
        patch.object(duckdb.DuckDBPyRelation, "to_arrow_reader", return_value=Reader()),
        pytest.raises(RuntimeError, match="read failed"),
    ):
        source.read_window(0, 1, control)
    assert closed.index("control") < closed.index("reader") < closed.index("connection")
    assert closed.count("reader") == closed.count("connection") == 1
