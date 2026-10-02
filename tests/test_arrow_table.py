"""Integration tests for table replacement and rendering."""

import asyncio

import pyarrow as pa
import pytest
from textual.app import App, ComposeResult
from textual.coordinate import Coordinate

from parqx.data.view import DataPage, TableData
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


async def test_window_requests_are_coalesced_and_partial_pages_fill_the_view() -> None:
    schema = pa.table({"value": [0]}).schema
    data = TableData(schema, 10_000)
    table = ArrowTable(data)
    requests: list[ArrowTable.WindowRequested] = []
    selected: list[ArrowTable.CellSelected] = []

    class TableApp(App[None]):
        def compose(self) -> ComposeResult:
            yield table

        def on_arrow_table_window_requested(
            self, event: ArrowTable.WindowRequested
        ) -> None:
            requests.append(event)

        def on_arrow_table_cell_selected(self, event: ArrowTable.CellSelected) -> None:
            selected.append(event)

    async with TableApp().run_test(size=(60, 12)) as pilot:
        await pilot.pause()
        assert len(requests) == 1
        assert requests[0].control is table
        assert requests[0].data is data
        assert (requests[0].start_row, requests[0].stop_row) == (0, 256)
        await pilot.press("enter")
        assert not selected
        assert len(requests) == 1

        # A byte-limited read may supply fewer rows than the requested window.
        table.accept_page(DataPage(0, pa.table({"value": [0, 1]})))
        await pilot.pause()
        assert len(requests) == 2
        assert requests[-1].start_row == 2
        table.accept_page(DataPage(2, pa.table({"value": range(2, 258)})))
        await pilot.pause()
        assert len(requests) == 2
        await pilot.press("enter")
        assert selected[-1].value.as_py() == 0

        table.move_cursor(row=9_000, animate=False)
        await pilot.pause()
        request = requests[-1]
        assert request.start_row > 8_900
        assert request.stop_row - request.start_row <= 256
        table.accept_page(
            DataPage(
                request.start_row,
                pa.table({"value": range(request.start_row, request.stop_row)}),
            )
        )
        await pilot.pause()
        assert table.cursor_coordinate == Coordinate(9_000, 0)
        assert table.get_cell_at(table.cursor_coordinate).as_py() == 9_000


async def test_failed_window_waits_for_navigation_before_retrying() -> None:
    table = ArrowTable(TableData(pa.table({"value": [0]}).schema, 1_000))
    requests: list[ArrowTable.WindowRequested] = []

    class TableApp(App[None]):
        def compose(self) -> ComposeResult:
            yield table

        def on_arrow_table_window_requested(
            self, event: ArrowTable.WindowRequested
        ) -> None:
            requests.append(event)
            table.fail_window()

    async with TableApp().run_test(size=(60, 12)) as pilot:
        await pilot.pause()
        assert len(requests) == 1
        table.refresh()
        await pilot.pause()
        assert len(requests) == 1
        await pilot.press("down")
        assert len(requests) == 2
        table.refresh()
        await pilot.pause()
        assert len(requests) == 2


async def test_small_cache_does_not_reload_visible_rows_forever() -> None:
    data = TableData(pa.table({"value": [0]}).schema, 1_000, cache_bytes=8)
    table = ArrowTable(data)
    requests: list[int] = []

    class TableApp(App[None]):
        def compose(self) -> ComposeResult:
            yield table

        def on_arrow_table_window_requested(
            self, event: ArrowTable.WindowRequested
        ) -> None:
            requests.append(event.start_row)
            assert len(requests) < 50, "Evicted rows must not cause a repaint/read loop"
            table.accept_page(
                DataPage(event.start_row, pa.table({"value": [event.start_row]}))
            )

    async with TableApp().run_test(size=(60, 12)) as pilot:

        async def wait_for_visible_rows() -> None:
            async with asyncio.timeout(5):
                while True:
                    await pilot.pause()
                    top = round(table.scroll_y)
                    if top <= table.cursor_row < top + table.size.height and all(
                        "…" not in table.render_line(row).text
                        for row in range(table.size.height)
                    ):
                        return

        await wait_for_visible_rows()
        assert 1 < len(requests) <= table.size.height
        count = len(requests)
        assert data.cache_bytes <= 8
        table.refresh()
        await pilot.pause()
        assert len(requests) == count

        table.move_cursor(row=950, animate=False)
        await wait_for_visible_rows()
        assert count < len(requests) <= count + table.size.height + 1
        assert data.cache_bytes <= 8
        count = len(requests)
        table.refresh()
        await pilot.pause()
        assert len(requests) == count
