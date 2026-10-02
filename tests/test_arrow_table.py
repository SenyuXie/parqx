"""Integration tests for table replacement and rendering."""

import pyarrow as pa
import pytest
from textual.app import App, ComposeResult
from textual.coordinate import Coordinate

from parqx.tui.widgets import ArrowTable
from parqx.tui.widgets.arrow_table import CursorType


@pytest.mark.parametrize("cursor_type", ["cell", "row", "column", "none"])
async def test_replace_table_resets_mounted_layout(cursor_type: CursorType) -> None:
    table = ArrowTable(
        pa.table({f"old_{index}": list(range(200)) for index in range(30)}),
        cursor_type=cursor_type,
        zebra_stripes=True,
        cell_padding=2,
    )

    class TableApp(App[None]):
        def compose(self) -> ComposeResult:
            yield table

    async with TableApp().run_test(size=(60, 12)) as pilot:
        table.move_cursor(row=150, column=25, animate=False)
        await pilot.pause()
        # Row and column cursors only scroll one axis; exercise both explicitly.
        table.scroll_to(x=100, y=100, animate=False, force=True)
        await pilot.pause()
        await pilot.hover(table, offset=(12, 3))
        assert table.scroll_x > 0
        assert table.scroll_y > 0
        if cursor_type != "none":
            assert table.hover_coordinate != Coordinate(0, 0)
        old_size = table.virtual_size

        table.replace_table(pa.table({"new": ["replacement", "second"]}))
        await pilot.pause()

        assert (table.row_count, table.column_count) == (2, 1)
        assert table.columns[0].name == "new"
        assert table.columns[0].content_width == len("replacement")
        assert table.index_column.content_width == 1
        assert table.virtual_size.height == 3  # Two data rows and the header.
        assert table.virtual_size.width < old_size.width
        assert (table.scroll_x, table.scroll_y) == (0, 0)
        assert table.cursor_coordinate == Coordinate(0, 0)
        assert table.hover_coordinate == Coordinate(0, 0)
        assert table.cursor_type == cursor_type
        assert table.zebra_stripes is True
        assert table.cell_padding == 2
        assert "new" in table.render_line(0).text
        assert "replacement" in table.render_line(1).text

        # Empty results still expose their schema and can be replaced again.
        table.replace_table(pa.table({"empty": pa.array([], type=pa.int64())}))
        await pilot.pause()
        assert table.row_count == 0
        assert table.virtual_size.height == 1
        assert "empty" in table.render_line(0).text

        table.replace_table(pa.table({"restored": [42]}))
        await pilot.pause()
        assert table.virtual_size.height == 2
        assert "restored" in table.render_line(0).text
        assert "42" in table.render_line(1).text
