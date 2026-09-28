from pathlib import Path
from unittest.mock import patch

import pytest
from textual.coordinate import Coordinate
from textual.widgets import TextArea

from parqx.query.engine import QuerySession
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from tests.helpers import WorkerGate, toggle_sql_query, wait_for


async def test_native_query_editing_and_focus(small_parquet: Path) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        table = app.query_one(ArrowTable)
        await wait_for(lambda: not table.loading, pilot)
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
        assert editor.has_focus

        editor.load_text("SELECT 42\nFROM data")
        await pilot.press("f6")
        assert editor.selected_text == "SELECT 42"
        await pilot.press("f7")
        assert editor.selected_text == editor.text
        await pilot.press("ctrl+c")
        assert app.clipboard == "SELECT 42\nFROM data"
        await pilot.press("ctrl+x")
        assert editor.text == ""
        await pilot.press("ctrl+v")
        assert editor.text == "SELECT 42\nFROM data"
        await pilot.press("ctrl+z")
        assert editor.text == ""
        await pilot.press("ctrl+y")
        assert editor.text == "SELECT 42\nFROM data"

        editor.move_cursor((0, 0))
        await pilot.press("ctrl+right")
        assert editor.cursor_location == (0, 6)
        await pilot.press("right", "shift+end")
        assert editor.selected_text == "42"
        assert table.row_count == 5
        assert app._query_control is None  # pyright: ignore[reportPrivateUsage]


async def test_panel_toggle_preserves_selection_and_undo_history(
    small_parquet: Path,
) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        await wait_for(lambda: not app.query_one(ArrowTable).loading, pilot)
        await toggle_sql_query(pilot, "Show the SQL query panel")
        editor = app.query_one(TextArea)
        editor.load_text("SELECT ")
        await pilot.press("end", "4", "2")
        editor.history.checkpoint()
        await pilot.press("shift+left", "shift+left")
        selection = editor.selection
        assert editor.selected_text == "42"
        await toggle_sql_query(pilot, "Hide the SQL query panel")
        await toggle_sql_query(pilot, "Show the SQL query panel")
        assert app.query_one(TextArea) is editor
        assert editor.text == "SELECT 42"
        assert editor.selection == selection
        await pilot.press("ctrl+z")
        assert editor.text == "SELECT "
        await pilot.press("ctrl+y")
        assert editor.text == "SELECT 42"


@pytest.mark.parametrize("focus_sql", [False, True])
async def test_only_f1_runs_the_complete_sql(
    small_parquet: Path, focus_sql: bool
) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        table = app.query_one(ArrowTable)
        await wait_for(lambda: not table.loading, pilot)
        app.action_toggle_query()
        editor = app.query_one(TextArea)
        editor.load_text("SELECT 42")
        await pilot.press("end", "shift+left", "shift+left")
        assert editor.selected_text == "42"
        if not focus_sql:
            table.focus()
        data = table.data
        await pilot.press("ctrl+enter", "f5", "f2", "f4")
        assert table.data is data
        assert editor.text == "SELECT 42"
        assert app._query_control is None  # pyright: ignore[reportPrivateUsage]
        await pilot.press("f1")
        await wait_for(lambda: not app.query_running, pilot)
        assert app.query_error is None
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == 42


@pytest.mark.parametrize("focus_sql", [False, True])
async def test_escape_leaves_running_query_and_f2_cancels(
    small_parquet: Path, focus_sql: bool
) -> None:
    gate = WorkerGate()
    original_enter = QuerySession.__enter__

    def slow_enter(session: QuerySession) -> QuerySession:
        gate.pause()
        return original_enter(session)

    app = ParqxApp(small_parquet)
    with patch.object(QuerySession, "__enter__", slow_enter):
        async with app.run_test() as pilot:
            with gate:
                table = app.query_one(ArrowTable)
                await wait_for(lambda: not table.loading, pilot)
                app.action_toggle_query()
                editor = app.query_one(TextArea)
                editor.load_text("SELECT 42")
                await pilot.press("f1")
                await wait_for(gate.started.is_set, pilot)
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
                gate.release.set()
                await wait_for(control.finished.is_set, pilot)
                assert table.row_count == 5
