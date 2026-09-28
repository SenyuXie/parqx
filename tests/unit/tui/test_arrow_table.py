# Inspect render internals to enforce the viewport work budget.
# pyright: reportPrivateUsage=false

from unittest.mock import patch

import pyarrow as pa
import pytest
from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.app import App
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


async def test_rendered_cells_preserve_width_metadata_and_cursor_visibility() -> None:
    widget = ArrowTable(pa.table({"x": ["long中value"]}))
    app = App[None]()
    async with app.run_test() as pilot:
        await app.mount(widget)
        await pilot.pause()
        widget._set_hover_cursor(True)
        ordinary = widget._render_cell(0, 0, Style(), width=8)
        highlighted = widget._render_cell(0, 0, Style(), width=8, cursor=True)
        hovered = widget._render_cell(0, 0, Style(), width=8, hover=True)
        for segments in (ordinary, highlighted, hovered):
            assert Segment.get_line_length(segments) == 8
            assert "…" in "".join(segment.text for segment in segments)
            assert all(
                segment.style is not None
                and segment.style.meta == {"row": 0, "column": 0}
                for segment in segments
            )
        assert highlighted != ordinary
        assert hovered != ordinary
        widget.show_cursor = False
        assert (
            widget._render_cell(0, 0, Style(), width=8, cursor=True, hover=True)
            == ordinary
        )


async def test_geometry_and_style_changes_reuse_formatted_values() -> None:
    widget = ArrowTable(pa.table({"x": ["value"]}))
    app = App[None]()
    async with app.run_test() as pilot:
        await app.mount(widget)
        await pilot.pause()
        before = widget.render_line(1)
        with patch.object(
            CellFormatter, "__call__", side_effect=AssertionError("format")
        ):
            widget.cell_padding = 3
            widget.zebra_stripes = True
            await pilot.pause()
            after = widget.render_line(1)
            assert after.text != before.text
            assert "value" in after.text
            widget.show_row_index = False
            await pilot.pause()
            assert widget.render_line(1).text.startswith("   value")
