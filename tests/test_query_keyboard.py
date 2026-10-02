"""SQL modal editing, execution, and dismissal shortcuts."""

from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest
from textual import events
from textual.coordinate import Coordinate
from textual.widgets import TabbedContent

from parqx.query.engine import QuerySession
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from tests.test_query_app import open_query, wait_for


async def test_shift_enter_and_multiline_paste_only_edit_sql(
    small_parquet: Path,
) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        table = app.query_one(ArrowTable)
        tabs = app.query_one(TabbedContent)
        query = await open_query(app, pilot)
        editor = query.editor
        editor.load_text("")
        await pilot.press("tab", "h", "i", "z", "c", "shift+enter", "x")
        assert editor.text == "    hizc\nx"
        assert editor.has_focus
        assert table.show_header
        assert table.show_row_index
        assert not table.zebra_stripes
        assert table.cursor_type == "cell"
        assert not query.running

        editor.load_text("")
        app.post_message(events.Paste("SELECT 42\nAS answer"))
        await wait_for(lambda: editor.text == "SELECT 42\nAS answer", pilot)
        assert not query.running
        assert tabs.tab_count == 1
        assert app.screen is query
        await pilot.press("escape")
        await wait_for(lambda: app.screen is not query, pilot)
        assert tabs.tab_count == 1


async def test_enter_runs_entire_sql_even_with_selection(small_parquet: Path) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        tabs = app.query_one(TabbedContent)
        query = await open_query(app, pilot)
        editor = query.editor
        editor.load_text("SELECT 42 AS answer")
        editor.move_cursor((0, 7))
        await pilot.press("shift+right", "shift+right")
        assert editor.selected_text == "42"
        await pilot.press("enter")
        await wait_for(lambda: app.screen is not query and tabs.tab_count == 2, pilot)
        table = tabs.get_pane("query-1").query_one(ArrowTable)
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == 42
        assert table.columns[0].name == "answer"


async def test_blank_sql_and_old_query_keys_do_not_execute(small_parquet: Path) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        tabs = app.query_one(TabbedContent)
        query = await open_query(app, pilot)
        query.editor.load_text(" \n ")
        await pilot.press("enter")
        assert not query.running
        assert query.error is None
        assert app.screen is query
        query.editor.load_text("SELECT 42")
        await pilot.press("f1", "f2", "f3")
        assert not query.running
        assert tabs.tab_count == 1
        assert app.screen is query


async def test_ctrl_w_in_editor_deletes_word_without_closing_tab(
    small_parquet: Path,
) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        tabs = app.query_one(TabbedContent)
        query = await open_query(app, pilot)
        query.editor.load_text("SELECT obsolete")
        query.editor.move_cursor((0, 15))
        await pilot.press("ctrl+w")
        assert query.editor.text == "SELECT "
        assert tabs.tab_count == 1
        assert app.screen is query
        assert query.editor.has_focus


async def test_running_query_blocks_edits_and_duplicate_execution(
    small_parquet: Path,
) -> None:
    started, release = Event(), Event()
    original_enter = QuerySession.__enter__
    calls = 0

    def delayed_enter(session: QuerySession) -> QuerySession:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=5)
        return original_enter(session)

    app = ParqxApp(small_parquet)
    with patch.object(QuerySession, "__enter__", delayed_enter):
        async with app.run_test() as pilot:
            try:
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                tabs = app.query_one(TabbedContent)
                query = await open_query(app, pilot)
                query.editor.load_text("SELECT 42")
                await pilot.press("enter")
                await wait_for(started.is_set, pilot)
                await pilot.press("enter", "enter", "x", "shift+enter", "backspace")
                assert calls == 1
                assert query.running
                assert query.editor.text == "SELECT 42"
                assert query.query_one("#query-loading").display
                await pilot.press("escape")
                await wait_for(lambda: app.screen is not query, pilot)
                assert not query.running
                assert tabs.tab_count == 1
            finally:
                release.set()


@pytest.mark.parametrize("size", [(100, 35), (40, 12)])
async def test_sql_dialog_fits_and_centers_in_terminal(
    small_parquet: Path, size: tuple[int, int]
) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test(size=size) as pilot:
        query = await open_query(app, pilot)
        dialog = query.query_one("#query-dialog")
        await wait_for(lambda: dialog.region.width > 0, pilot)
        region = dialog.region
        assert 0 <= region.x < region.right <= size[0]
        assert 0 <= region.y < region.bottom <= size[1]
        assert abs(region.x - (size[0] - region.right)) <= 1
        assert abs(region.y - (size[1] - region.bottom)) <= 1
        assert query.editor.region.height > 0
        await pilot.press("escape")
        await wait_for(lambda: app.screen is not query, pilot)
