# Inspect render internals to enforce the viewport work budget.
# pyright: reportPrivateUsage=false

import weakref
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from rich.cells import cell_len
from rich.text import Text
from textual.coordinate import Coordinate

from parqx.data.view import DataPage, TableData
from parqx.tui.cell_formatter import CellFormatter
from parqx.tui.widgets.arrow_table import ArrowTable, CellNotLoadedError


def _column_width_peak_memory(table: pa.Table) -> int:
    """Measure Arrow allocations during the first column-width calculation."""
    widget = ArrowTable(table)
    assert widget._columns is None
    original_pool = pa.default_memory_pool()
    pool = pa.proxy_memory_pool(original_pool)
    try:
        pa.set_memory_pool(pool)
        _ = widget.columns
    finally:
        pa.set_memory_pool(original_pool)

    # Keep the proxy alive until all temporary Arrow buffers have been released.
    assert pool.bytes_allocated() == 0
    peak = pool.max_memory()
    assert peak is not None
    return peak


def test_measure_short_binary_values() -> None:
    table = ArrowTable(pa.table({"x": [b"\x00\x01\x02", b"", None]}))

    assert table.columns[0].content_width == 8


@pytest.mark.parametrize(
    ("values", "data_type"),
    [
        ([0, 12345], pa.duration("us")),
        ([(0, 0, 0), (1, 2, 3)], pa.month_day_nano_interval()),
    ],
)
def test_measure_temporal_types_without_min_max_kernel(
    values: list[object], data_type: pa.DataType
) -> None:
    array = pa.array(values, type=data_type)
    table = ArrowTable(pa.table({"x": array}))
    formatter = CellFormatter(inline_limit=48)
    expected = max(cell_len(formatter(scalar).plain) for scalar in array)

    assert table.columns[0].content_width == expected


def test_numeric_measurement_includes_null_width() -> None:
    table = ArrowTable(pa.table({"x": pa.array([1, None], type=pa.int64())}))

    assert table.columns[0].content_width == 4


def test_sample_measurement_includes_unsampled_null_width() -> None:
    values: list[str | None] = ["x"] * 257
    values[128] = None
    table = ArrowTable(pa.table({"x": values}))

    assert table.columns[0].content_width == 4


def test_wide_table_formats_only_accessed_cells() -> None:
    widget = ArrowTable(pa.table({f"c{i}": ["value"] for i in range(500)}))
    _ = widget.columns
    with patch.object(CellFormatter, "__call__", return_value=Text("value")) as fmt:
        assert widget._get_cell_renderable(0, 0).plain == "value"
        widget._get_cell_renderable(0, 0)
        widget._get_cell_renderable(0, -1)
        assert fmt.call_count == 1
        widget._get_cell_renderable(0, 499)
        assert fmt.call_count == 2


def test_numeric_width_measurement_is_bounded() -> None:
    with (
        patch("pyarrow.compute.min_max", side_effect=AssertionError("full scan")),
        patch.object(CellFormatter, "__call__", return_value=Text("42")) as fmt,
    ):
        widget = ArrowTable(pa.table({"x": range(100_000)}))
        assert widget.columns[0].content_width == 2
        assert fmt.call_count <= 256


@pytest.mark.parametrize("chunk_count", [16, 256], ids=["1m-rows", "16m-rows"])
def test_chunked_width_measurement_has_bounded_memory(chunk_count: int) -> None:
    # Reuse a small buffer so input construction does not need a large allocation.
    chunk = pa.array(range(62_500), type=pa.int64())
    column = pa.chunked_array([chunk] * chunk_count)
    table = pa.table({"x": column})

    assert _column_width_peak_memory(table) < 64 * 1024


def test_parquet_width_measurement_has_bounded_memory(tmp_path: Path) -> None:
    chunk = pa.array(range(62_500), type=pa.int64())
    source = pa.table({"x": pa.chunked_array([chunk] * 16)})
    path = tmp_path / "chunked.parquet"
    pq.write_table(source, path, row_group_size=62_500)
    table = pq.read_table(path)
    assert table.column("x").num_chunks > 1

    assert _column_width_peak_memory(table) < 64 * 1024


@pytest.mark.parametrize(
    ("values", "expected_width"),
    [
        ([1] * 128 + [None] + [123456] * 128, 6),
        (
            [datetime(2026, 1, 1)] * 128
            + [None]
            + [datetime(2026, 10, 2, 12, 34, 56, 123456)] * 128,
            26,
        ),
        (["界e\u0301"] * 128 + [None] + ["你好你好"] * 128, 8),
        ([None] * 257, 4),
    ],
    ids=["integer", "timestamp", "unicode", "null"],
)
def test_width_measurement_is_independent_of_chunk_layout(
    values: list[object], expected_width: int
) -> None:
    array = pa.array(values)
    # Row 128 is outside the 256-row sample and occupies its own chunk.
    chunked = pa.chunked_array(
        [[], values[:128], [], values[128:129], values[129:], []], type=array.type
    )
    single_widget = ArrowTable(pa.table({"x": array}))
    chunked_widget = ArrowTable(pa.table({"x": chunked}))

    assert single_widget.columns[0].content_width == expected_width
    assert chunked_widget.columns[0].content_width == expected_width


def test_replace_table_invalidates_content_schema_and_layout() -> None:
    widget = ArrowTable(pa.table({"before": ["old", "other"]}))
    assert widget._get_cell_renderable(0, 0).plain == "old"
    assert widget._get_cell_renderable(-1, 0).plain == "before"
    old_offsets = widget._get_column_offsets()
    widget.replace_table(pa.table({"after": ["a much longer value"], "new": [42]}))
    assert widget._get_cell_renderable(0, 0).plain == "a much longer value"
    assert widget._get_cell_renderable(-1, 0).plain == "after"
    assert widget._get_column_offsets() != old_offsets
    assert (widget.row_count, widget.column_count) == (1, 2)
    assert widget.cursor_coordinate == Coordinate(0, 0)
    widget.replace_table(pa.table({"empty": pa.array([], type=pa.int64())}))
    assert widget.row_count == 0
    assert widget.columns[0].name == "empty"


def test_unloaded_cells_are_distinct_from_null_and_do_not_cache_placeholders() -> None:
    page = pa.table({"value": pa.array([None, 12345], type=pa.int64())})
    widget = ArrowTable(TableData(page.schema, 10_000))
    assert widget.row_count == 10_000
    assert widget.columns[0].content_width == 0
    with pytest.raises(CellNotLoadedError):
        widget.get_cell_at(Coordinate(0, 0))
    assert widget._get_cell_renderable(0, 0).plain == "…"
    assert Coordinate(0, 0) not in widget._cell_renderable_cache

    widget.accept_page(DataPage(0, page))
    assert not widget.get_cell_at(Coordinate(0, 0)).is_valid
    assert widget._get_cell_renderable(0, 0).plain == "null"
    assert widget._get_cell_renderable(1, 0).plain == "12345"
    assert widget.columns[0].content_width == 5
    assert widget.row_count == 10_000


def test_replacing_window_data_discards_cached_cells_and_pending_requests() -> None:
    page = pa.table({"old": ["before"]})
    data = TableData(page.schema, 1_000)
    widget = ArrowTable(data)
    widget.accept_page(DataPage(0, page))
    assert widget._get_cell_renderable(0, 0).plain == "before"
    widget.fail_window()

    widget.replace_data(TableData.from_table(pa.table({"new": ["after"]})))
    assert widget.data is not data
    assert widget._get_cell_renderable(0, 0).plain == "after"
    assert widget.columns[0].name == "new"
    assert widget.row_count == 1
    assert not widget._window_failed
    assert widget._requested_window is None


def test_page_widths_match_loaded_data_and_refresh_cached_layout() -> None:
    table = pa.table(
        {
            "id": [123456789, None],
            "text": ["界e\u0301", "你好你好"],
            "time": pa.array([123456789, None], type=pa.timestamp("ns", tz="UTC")),
            "null": [None, None],
            "flag": [False, None],
            "payload": ["hello " * 50_000, "x" * 300_000],
        }
    )
    widget = ArrowTable(TableData(table.schema, table.num_rows))
    old_offsets = widget._get_column_offsets()
    widget.accept_page(DataPage(0, table))

    assert widget.columns == ArrowTable(table).columns
    assert widget.columns[0].content_width == 9
    assert widget._get_column_offsets() != old_offsets


def test_page_width_measurement_is_bounded_and_does_not_retain_arrow_samples() -> None:
    # A large multi-chunk page must not be combined or copied just for widths.
    chunk = pa.array(["hello " * 50_000])
    table = pa.table({"text": pa.chunked_array([chunk] * 512)})
    widget = ArrowTable(TableData(table.schema, 1024, cache_bytes=1))
    reference = weakref.ref(table)
    next_page = DataPage(512, pa.table({"text": ["next"]}))
    calls = 0
    original_format = CellFormatter.__call__

    def count_format(formatter: CellFormatter, scalar: pa.Scalar) -> Text:
        nonlocal calls
        calls += 1
        return original_format(formatter, scalar)

    original_pool = pa.default_memory_pool()
    pool = pa.proxy_memory_pool(original_pool)
    try:
        pa.set_memory_pool(pool)
        with patch.object(CellFormatter, "__call__", count_format):
            widget.accept_page(DataPage(0, table))
            assert calls == 256
            widths = widget.columns
            widget.accept_page(next_page)
            assert calls == 256
        assert widget.columns == widths
        peak = pool.max_memory()
        del table
        assert reference() is None
        assert widget.data.peek(0, 0) is None
    finally:
        pa.set_memory_pool(original_pool)
    assert pool.bytes_allocated() == 0
    assert peak is not None
    assert peak < 64 * 1024


def test_empty_page_and_replacement_allow_first_content_width_measurement() -> None:
    table = pa.table({"n": [12345]})
    widget = ArrowTable(TableData(table.schema, 1))
    widget.accept_page(DataPage(0, table.slice(0, 0)))
    assert widget.columns[0].content_width == 0
    widget.accept_page(DataPage(0, table))
    assert widget.columns[0].content_width == 5

    replacement = pa.table({"n": [123456789]})
    widget.replace_data(TableData(replacement.schema, 1))
    assert widget.columns[0].content_width == 0
    widget.accept_page(DataPage(0, replacement))
    assert widget.columns[0].content_width == 9
