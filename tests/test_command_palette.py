"""SQL panel discovery and modal command palette behavior."""

from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch

import pytest
from textual.command import Command, CommandList, CommandPalette
from textual.coordinate import Coordinate
from textual.pilot import Pilot
from textual.widgets import Button, Input, TextArea

from parqx.query.engine import QuerySession
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable, QueryPanel
from tests.test_query_app import wait_for


async def toggle_sql_query(pilot: Pilot[Any], help_text: str) -> None:
    await pilot.press("ctrl+p")
    palette = pilot.app.screen
    assert isinstance(palette, CommandPalette)
    palette.query_one(Input).value = "SQL"
    commands = palette.query_one(CommandList)

    def found() -> bool:
        if commands.option_count != 1:
            return False
        option = commands.get_option_at_index(0)
        return isinstance(option, Command) and option.hit.text == "SQL query"

    await wait_for(found, pilot)
    option = commands.get_option_at_index(0)
    assert isinstance(option, Command)
    assert option.hit.help == help_text
    await pilot.press("enter")
    await wait_for(lambda: pilot.app.screen is not palette, pilot)
    await pilot.pause()


async def test_palette_toggle_preserves_editor_and_query_result(
    small_parquet: Path,
) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        table = app.query_one(ArrowTable)
        panel = app.query_one(QueryPanel)
        editor = app.query_one(TextArea)
        assert not panel.display
        assert not panel.query(Button)
        assert editor.border_subtitle == "F1 Run · F2 Cancel · F3 Browse · Esc Back"
        assert {"Theme", "Quit", "Keys", "SQL query"} <= {
            command.title for command in app.get_system_commands(app.screen)
        }

        await toggle_sql_query(pilot, "Show the SQL query panel")
        assert editor.has_focus
        editor.load_text("SELECT ")
        await pilot.press("end", "4", "2")
        editor.history.checkpoint()
        await pilot.press("shift+left", "shift+left")
        selection = editor.selection
        assert editor.selected_text == "42"
        await toggle_sql_query(pilot, "Hide the SQL query panel")
        assert not panel.display
        assert table.has_focus
        await toggle_sql_query(pilot, "Show the SQL query panel")
        assert app.query_one(TextArea) is editor
        assert editor.has_focus
        assert editor.selection == selection
        assert editor.text == "SELECT 42"
        assert table.row_count == 5
        await pilot.press("ctrl+z")
        assert editor.text == "SELECT "
        await pilot.press("ctrl+y", "f1")
        await wait_for(lambda: not app.query_running, pilot)
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == 42

        await toggle_sql_query(pilot, "Hide the SQL query panel")
        await toggle_sql_query(pilot, "Show the SQL query panel")
        assert editor.text == "SELECT 42"
        assert table.row_count == 1
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == 42


@pytest.mark.parametrize("query_error", [False, True])
async def test_query_finishes_under_palette_without_stealing_focus(
    small_parquet: Path, query_error: bool
) -> None:
    started, release = Event(), Event()
    original_enter = QuerySession.__enter__

    def delayed_enter(session: QuerySession) -> QuerySession:
        started.set()
        release.wait(timeout=5)
        return original_enter(session)

    sql = "SELECT missing FROM data" if query_error else "SELECT 42 AS answer"
    app = ParqxApp(small_parquet, initial_sql=sql)
    with patch.object(QuerySession, "__enter__", delayed_enter):
        async with app.run_test() as pilot:
            try:
                await wait_for(started.is_set, pilot)
                await pilot.press("ctrl+p")
                palette = app.screen
                assert isinstance(palette, CommandPalette)
                control = app._query_control  # pyright: ignore[reportPrivateUsage]
                assert control is not None
                await pilot.press("f1", "f2", "f3")
                assert app._query_control is control  # pyright: ignore[reportPrivateUsage]
                assert not control.cancelled.is_set()
                assert app.query_running
                for action in ("run_query", "cancel_query", "browse"):
                    assert app.check_action(action, ()) is False

                release.set()
                await wait_for(lambda: not app.query_running, pilot)
                assert app.screen is palette
                assert palette.query_one(Input).has_focus
                assert (app.query_error is not None) == query_error
                await pilot.press("escape")
                assert app.screen is not palette
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                assert app.query_one(ArrowTable).row_count == (0 if query_error else 1)
            finally:
                release.set()
