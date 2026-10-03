"""SQL modal discovery and persistent editing through the command palette."""

from pathlib import Path
from typing import Any

from textual.command import Command, CommandList, CommandPalette
from textual.pilot import Pilot
from textual.widgets import Input, TabbedContent

from parqx.tui.app import ParqxApp
from parqx.tui.screens.query import QueryScreen
from parqx.tui.widgets import ArrowTable
from tests.test_query_app import wait_for


async def open_sql_query(pilot: Pilot[Any]) -> QueryScreen:
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
    await pilot.press("enter")
    await wait_for(lambda: isinstance(pilot.app.screen, QueryScreen), pilot)
    query = pilot.app.screen
    assert isinstance(query, QueryScreen)
    await wait_for(lambda: query.editor.has_focus, pilot)
    return query


async def test_palette_open_preserves_editor_selection_and_history(
    small_parquet: Path,
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        tabs = app.query_one(TabbedContent)
        titles = {command.title for command in app.get_system_commands(app.screen)}
        assert {"Theme", "Quit", "Keys", "SQL query"} <= titles
        assert "Original data" not in titles
        query = await open_sql_query(pilot)
        editor = query.editor
        editor.load_text("SELECT ")
        await pilot.press("end", "4", "2")
        editor.history.checkpoint()
        await pilot.press("shift+left", "shift+left")
        selection = editor.selection
        assert editor.selected_text == "42"
        await pilot.press("escape")
        await wait_for(lambda: app.screen is not query, pilot)
        await open_sql_query(pilot)
        assert query.editor is editor
        assert editor.selection == selection
        assert editor.text == "SELECT 42"
        assert tabs.tab_count == 1
        await pilot.press("ctrl+z")
        assert editor.text == "SELECT "
        await pilot.press("ctrl+y", "enter")
        await wait_for(lambda: app.screen is not query and tabs.tab_count == 2, pilot)
        await open_sql_query(pilot)
        assert editor.text == "SELECT 42"
        assert tabs.tab_count == 2


async def test_palette_cannot_stack_above_query_dialog(small_parquet: Path) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test() as pilot:
        query = await open_sql_query(pilot)
        await pilot.press("ctrl+p")
        assert app.screen is query
        assert query.editor.has_focus
        assert app.check_action("close_tab", ()) is False
        await pilot.press("escape")
        await wait_for(lambda: app.screen is not query, pilot)
        await pilot.press("ctrl+p")
        assert isinstance(app.screen, CommandPalette)
