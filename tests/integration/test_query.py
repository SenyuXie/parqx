"""SQL user journeys: results, editing, discovery, and tab ownership."""

import gc
import weakref
from pathlib import Path
from typing import Any
from unittest.mock import patch

from textual import events
from textual._xterm_parser import XTermParser
from textual.command import Command, CommandList, CommandPalette
from textual.coordinate import Coordinate
from textual.pilot import Pilot
from textual.widgets import Footer, Input, TabbedContent

from parqx.config import ParqxConfig
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from parqx.tui.widgets.query_workspace import QueryWorkspace
from tests.helpers import (
    footer_keys,
    open_query,
    run_query,
    select_tab,
    wait_for,
    wait_for_query_error,
)


async def test_bounded_results_and_error_recovery(small_parquet: Path) -> None:
    app = ParqxApp([small_parquet], config=ParqxConfig(preview_rows=2))
    async with app.run_test() as pilot:
        tabs = app.query_one("#results", TabbedContent)
        await wait_for(lambda: not tabs.get_pane("source-1").loading, pilot)
        source = tabs.get_pane("source-1").query_one(ArrowTable)
        assert str(tabs.get_tab("source-1").label) == small_parquet.name
        assert (source.row_count, source.column_count) == (5, 3)
        table = await run_query(app, pilot, "SELECT name FROM smoke ORDER BY id")
        query = tabs.active_pane
        assert isinstance(query, QueryWorkspace)
        assert (table.row_count, table.column_count) == (2, 1)
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == "alice"
        tooltip = str(tabs.get_tab("query-1").tooltip)
        assert "2 rows" in tooltip
        assert "preview" in tooltip
        assert "total unknown" in tooltip
        query.editor.focus()
        query.editor.load_text('SELECT "missing [bold]汉字[/bold]" FROM smoke')
        with patch.object(app, "notify") as notify:
            await pilot.press("ctrl+enter")
            message = await wait_for_query_error(notify, query, pilot)
            assert "missing [bold]汉字[/bold]" in message
            assert message in str(query.status.content)
            assert query.editor.has_focus
            assert not query.editor.read_only
            assert tabs.tab_count == 2
            assert table.row_count == 2
            assert query.current_result is not None
            assert query.current_result.sql == "SELECT name FROM smoke ORDER BY id"
            query.editor.load_text("SELECT name FROM smoke WHERE false")
            await pilot.press("ctrl+enter")
            await wait_for(lambda: table.row_count == 0 and not query.running, pilot)
            assert table.columns[0].name == "name"
            assert query.error is None
            assert tabs.tab_count == 2
            assert source.row_count == 5
            assert notify.call_count == 1


async def test_tabs_preserve_state_and_release_data_on_close(
    small_parquet: Path,
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test(size=(60, 15)) as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        tabs = app.query_one(TabbedContent)
        footer = app.query_one(Footer)
        await wait_for(lambda: "close_tab" in footer_keys(footer), pilot)
        assert footer_keys(footer)["close_tab"].has_class("-disabled")
        await pilot.press("ctrl+w")
        assert tabs.tab_count == 1

        first = await run_query(app, pilot, "SELECT i FROM range(100) t(i)")
        await pilot.press("h", "i", "z", "c")
        first.move_cursor(row=80, column=0, animate=False)
        await wait_for(lambda: first.scroll_y > 0, pilot)
        first_state = (
            first.show_header,
            first.show_row_index,
            first.zebra_stripes,
            first.cursor_type,
            first.cursor_coordinate,
            first.scroll_y,
        )
        second = await run_query(app, pilot, "SELECT 42 AS answer")
        assert second.show_header
        assert second.show_row_index
        assert not second.zebra_stripes
        assert second.cursor_type == "cell"
        await select_tab(tabs, "query-1", pilot)
        assert (
            first.show_header,
            first.show_row_index,
            first.zebra_stripes,
            first.cursor_type,
            first.cursor_coordinate,
            first.scroll_y,
        ) == first_state

        await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]

        # Source windows and SQL buffers have different owners; both must be released.
        async def close_and_release(pane_id: str) -> None:
            data = weakref.ref(tabs.get_pane(pane_id).query_one(ArrowTable).data)
            await select_tab(tabs, pane_id, pilot)
            await pilot.press("ctrl+w")
            await wait_for(lambda: not tabs.query(f"#{pane_id}"), pilot)

            def released() -> bool:
                gc.collect()
                return data() is None

            await wait_for(released, pilot)

        await close_and_release("query-1")
        await close_and_release("source-1")
        assert tabs.active == "query-2"
        assert tabs.tab_count == 1
        await wait_for(
            lambda: footer_keys(footer)["close_tab"].has_class("-disabled"), pilot
        )
        await pilot.press("ctrl+w")
        assert tabs.tab_count == 1
        # A closed source tab remains available to SQL through the catalog.
        count = await run_query(app, pilot, "SELECT count(*) FROM smoke")
        assert count.get_cell_at(Coordinate(0, 0)).as_py() == 5


async def test_unavailable_sources_report_details_and_recover(
    small_parquet: Path,
) -> None:
    contents = small_parquet.read_bytes()
    app = ParqxApp([small_parquet])
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
        small_parquet.unlink()
        await run_query(app, pilot, "SELECT 42 AS answer")
        tabs = app.query_one(TabbedContent)
        tooltip = str(tabs.get_tab("query-1").tooltip)
        assert "warning" in tooltip
        assert str(small_parquet) in tooltip
        assert '"smoke"' in tooltip
        query = await open_query(app, pilot)
        query.editor.load_text("SELECT * FROM smoke")
        with patch.object(app, "notify", wraps=app.notify) as notify:
            await pilot.press("ctrl+enter")
            message = await wait_for_query_error(notify, query, pilot)
        assert "Unavailable sources" in message
        assert str(small_parquet) in message
        small_parquet.write_bytes(contents)
        query.editor.load_text("SELECT count(*) FROM smoke")
        await pilot.press("ctrl+enter")
        await wait_for(
            lambda: bool(query.query(ArrowTable)) and not query.running, pilot
        )
        assert "warning" not in str(tabs.get_tab("query-2").tooltip)
        app.catalog.mark_failed("source-1", "Could not open this source")
        await run_query(app, pilot, "SELECT 42 AS answer")
        tooltip = str(tabs.get_tab("query-3").tooltip)
        assert "warning" in tooltip
        assert "Could not open this source" in tooltip


async def open_sql_query(pilot: Pilot[Any]) -> QueryWorkspace:
    await pilot.press("ctrl+p")
    palette = pilot.app.screen
    assert isinstance(palette, CommandPalette)
    palette.query_one(Input).value = "Query"
    commands = palette.query_one(CommandList)

    def found() -> bool:
        if commands.option_count != 1:
            return False
        option = commands.get_option_at_index(0)
        return isinstance(option, Command) and option.hit.text == "Query"

    await wait_for(found, pilot)
    await pilot.press("enter")
    await wait_for(lambda: not pilot.app.screen.is_modal, pilot)
    query = pilot.app.query_one(TabbedContent).active_pane
    assert isinstance(query, QueryWorkspace)
    await wait_for(lambda: query.editor.has_focus, pilot)
    return query


async def test_palette_preserves_editor_selection_and_history(
    small_parquet: Path,
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test() as pilot:
        query = await open_sql_query(pilot)
        editor = query.editor
        editor.load_text("SELECT ")
        await pilot.press("end", "4", "2")
        editor.history.checkpoint()
        await pilot.press("shift+left", "shift+left")
        selection = editor.selection
        assert editor.selected_text == "42"
        await pilot.press("ctrl+p")
        assert isinstance(app.screen, CommandPalette)
        assert app.check_action("close_tab", ()) is False
        await pilot.press("escape")
        await wait_for(lambda: editor.has_focus, pilot)
        assert editor.selection == selection
        assert editor.text == "SELECT 42"
        await pilot.press("ctrl+z")
        assert editor.text == "SELECT "
        await pilot.press("ctrl+y", "ctrl+enter")
        await wait_for(lambda: bool(query.query(ArrowTable)), pilot)
        assert query.query_one(ArrowTable).get_cell_at(Coordinate(0, 0)).as_py() == 42
        second = await open_sql_query(pilot)
        assert second.editor.text == ""
        assert editor.text == "SELECT 42"


async def test_compact_editor_keyboard_and_terminal_input(small_parquet: Path) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        source = app.query_one(ArrowTable)
        tabs = app.query_one(TabbedContent)
        query = await open_query(app, pilot)
        editor = query.editor
        footer = app.query_one(Footer)
        assert editor.region.height > 0
        assert 0 <= footer.region.y < footer.region.bottom <= 24
        editor.load_text(" \n ")
        await pilot.press("ctrl+enter")
        assert not query.running
        assert not query.query(ArrowTable)
        editor.load_text("")
        await pilot.press("tab", "h", "i", "z", "c", "enter", "x")
        assert editor.text == "    hizc\nx"
        assert editor.has_focus
        assert source.show_header
        assert source.show_row_index
        assert not source.zebra_stripes
        assert source.cursor_type == "cell"
        await pilot.press("shift+tab")
        assert not editor.has_focus
        assert editor.text == "    hizc\nx"
        editor.focus()
        editor.load_text("")
        app.post_message(events.Paste("SELECT 42\nAS answer"))
        await wait_for(lambda: editor.text == "SELECT 42\nAS answer", pilot)
        assert not query.running
        editor.move_cursor((0, 7))
        await pilot.press("shift+right", "shift+right")
        assert editor.selected_text == "42"
        for message in XTermParser().feed("\x1b[13;5u"):
            app.post_message(message)
        await wait_for(lambda: bool(query.query(ArrowTable)), pilot)
        table = query.query_one(ArrowTable)
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == 42
        assert table.columns[0].name == "answer"
        assert tabs.tab_count == 2
        await select_tab(tabs, "source-1", pilot)
        await pilot.press("ctrl+enter")
        assert tabs.tab_count == 2
        assert not query.running


async def test_query_editor_retains_height_through_multiline_edits(
    small_parquet: Path,
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test(size=(120, 40)) as pilot:
        query = await open_query(app, pilot)
        editor = query.editor
        await wait_for(lambda: editor.region.height > 0, pilot)
        initial_height = editor.region.height
        assert editor.content_region.height == 8
        await pilot.press("S", "E", "L", "E", "C", "T", "space", "4", "2")
        editor.history.checkpoint()
        await pilot.press("enter")
        assert editor.text == "SELECT 42\n"
        assert editor.region.height == initial_height
        await pilot.press("ctrl+z")
        assert editor.text == "SELECT 42"
        await pilot.press("ctrl+y")
        editor.history.checkpoint()
        app.post_message(events.Paste("AS answer\nFROM smoke"))
        await wait_for(lambda: editor.text == "SELECT 42\nAS answer\nFROM smoke", pilot)
        assert editor.region.height == initial_height
        await pilot.press("ctrl+z", "backspace")
        assert editor.text == "SELECT 42"
        assert editor.region.height == initial_height


async def test_query_editor_scrolls_within_resized_terminal(
    small_parquet: Path,
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test(size=(80, 24)) as pilot:
        query = await open_query(app, pilot)
        editor = query.editor
        footer = app.query_one(Footer)
        await wait_for(lambda: editor.region.height > 0, pilot)
        initial_height = editor.region.height
        long_line = "SELECT " + ", ".join(str(number) for number in range(100))
        app.post_message(events.Paste(long_line))
        await wait_for(lambda: editor.text == long_line and editor.scroll_x > 0, pilot)
        assert editor.region.height == initial_height
        editor.load_text("")
        multiline_sql = "SELECT 1" + "\n-- another line" * 49
        app.post_message(events.Paste(multiline_sql))
        await wait_for(
            lambda: editor.text == multiline_sql and editor.scroll_y > 0, pilot
        )
        assert editor.region.height == initial_height
        assert editor.cursor_location == (49, len("-- another line"))
        await pilot.resize_terminal(40, 12)
        await wait_for(lambda: query.region.width == 40, pilot)
        assert 0 < editor.region.height < query.region.height
        assert app.screen.region.contains_region(footer.region)
        await pilot.press("enter")
        await wait_for(
            lambda: editor.scrollable_content_region.contains(
                *editor.cursor_screen_offset
            ),
            pilot,
        )
        assert editor.text == multiline_sql + "\n"
        await pilot.press("f7", "backspace")
        assert editor.text == ""
        await pilot.resize_terminal(120, 40)
        await wait_for(lambda: editor.content_region.height == 8, pilot)
        assert editor.has_focus


async def test_query_editor_only_shows_scrollbar_when_lines_overflow(
    small_parquet: Path,
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test(size=(80, 24)) as pilot:
        query = await open_query(app, pilot)
        editor = query.editor
        await wait_for(lambda: editor.region.height > 0, pilot)
        await pilot.press("enter")
        assert not editor.show_vertical_scrollbar
        editor.history.checkpoint()
        app.post_message(events.Paste("SELECT 1\nAS answer"))
        await wait_for(lambda: editor.document.line_count == 3, pilot)
        assert not editor.show_vertical_scrollbar
        await pilot.press("ctrl+z")
        assert editor.document.line_count == 2
        assert not editor.show_vertical_scrollbar
        await pilot.press("f7")
        overflow_sql = "SELECT 1" + "\n-- another line" * 49
        app.post_message(events.Paste(overflow_sql))
        await wait_for(
            lambda: (
                editor.text == overflow_sql
                and editor.show_vertical_scrollbar
                and editor.scroll_y > 0
            ),
            pilot,
        )
        assert editor.region.height < 50
        assert editor.scrollable_content_region.contains(*editor.cursor_screen_offset)
