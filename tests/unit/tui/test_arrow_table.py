# Inspect render internals to enforce the viewport work budget.
# pyright: reportPrivateUsage=false

from unittest.mock import patch

import pyarrow as pa
import pytest
from rich.cells import cell_len
from rich.text import Text
from textual.coordinate import Coordinate

from parqx.tui.cell_formatter import CellFormatter
from parqx.tui.widgets.arrow_table import ArrowTable


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


def test_extrema_measurement_includes_null_width() -> None:
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
