"""Native SQL editor bindings and query shortcuts."""

from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest
from textual.coordinate import Coordinate
from textual.widgets import TextArea

from parqx.query.engine import QuerySession
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from tests.test_query_app import wait_for


async def test_native_query_editing_selection_and_focus(small_parquet: Path) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        table = app.query_one(ArrowTable)
        app.action_toggle_query()
        editor = app.query_one(TextArea)
        editor.load_text("")
        await pilot.press("tab", "h", "i", "z", "c", "enter", "x")
        assert editor.text == "    hizc\nx"
        assert editor.has_focus
        assert table.show_header
        assert table.show_row_index
        assert not table.zebra_stripes
        assert table.cursor_type == "cell"

        await pilot.press("escape")
        assert table.has_focus
        await pilot.press("tab")
        assert editor.has_focus
        await pilot.press("shift+tab")
        assert table.has_focus
        await pilot.press("tab")

        editor.load_text("SELECT 42\nFROM data")
        await pilot.press("f6")
        assert editor.selected_text == "SELECT 42"
        await pilot.press("f7")
        assert editor.selected_text == editor.text
        assert not app.query_running
        assert table.row_count == 5
        await pilot.press("ctrl+x", "ctrl+v")
        assert editor.text == "SELECT 42\nFROM data"
        await pilot.press("ctrl+z")
        assert editor.text == ""
        await pilot.press("ctrl+y")
        assert editor.text == "SELECT 42\nFROM data"


@pytest.mark.parametrize("focus_sql", [False, True])
async def test_f1_runs_full_sql_even_with_selection(
    small_parquet: Path, focus_sql: bool
) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        table = app.query_one(ArrowTable)
        app.action_toggle_query()
        editor = app.query_one(TextArea)
        editor.load_text("SELECT 42 AS answer")
        editor.move_cursor((0, 7))
        await pilot.press("shift+right", "shift+right")
        assert editor.selected_text == "42"
        if not focus_sql:
            table.focus()
        await pilot.press("f1")
        await wait_for(lambda: not app.query_running, pilot)
        assert app.query_error is None
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == 42
        assert table.columns[0].name == "answer"


@pytest.mark.parametrize("focus_sql", [False, True])
async def test_escape_keeps_query_running_and_f2_cancels(
    small_parquet: Path, focus_sql: bool
) -> None:
    started, release = Event(), Event()
    original_enter = QuerySession.__enter__

    def delayed_enter(session: QuerySession) -> QuerySession:
        started.set()
        release.wait(timeout=5)
        return original_enter(session)

    app = ParqxApp(small_parquet)
    with patch.object(QuerySession, "__enter__", delayed_enter):
        async with app.run_test() as pilot:
            try:
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                table = app.query_one(ArrowTable)
                app.action_toggle_query()
                editor = app.query_one(TextArea)
                editor.load_text("SELECT 42")
                await pilot.press("f1")
                await wait_for(started.is_set, pilot)
                control = app._query_control  # pyright: ignore[reportPrivateUsage]
                assert control is not None
                await pilot.press("escape")
                assert table.has_focus
                assert app.query_running
                assert not control.cancelled.is_set()
                if focus_sql:
                    await pilot.press("tab")
                    assert editor.has_focus
                await pilot.press("f2")
                assert not app.query_running
                assert control.cancelled.is_set()
                release.set()
                await wait_for(control.finished.is_set, pilot)
                assert table.row_count == 5
                assert app.query_error is None
            finally:
                release.set()
