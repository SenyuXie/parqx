"""Shared UI actions; wait for observable state instead of fixed delays."""

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import Mock

from textual.command import Command, CommandList, CommandPalette
from textual.pilot import Pilot
from textual.widgets import Button, Input, TabbedContent, Tabs

from parqx.tui.app import Parqx
from parqx.tui.widgets import ArrowTable
from parqx.tui.widgets.query_workspace import QueryWorkspace


async def wait_for(predicate: Callable[[], bool], pilot: Pilot[Any]) -> None:
    """Wait for an observable worker result while pumping the UI."""
    async with asyncio.timeout(5):
        while not predicate():
            await pilot.pause()


async def wait_for_query_error(
    notify: Mock, query: QueryWorkspace, pilot: Pilot[Any]
) -> str:
    await wait_for(lambda: notify.called and not query.running, pilot)
    assert notify.call_args is not None
    message = notify.call_args.args[0]
    assert isinstance(message, str)
    notify.assert_called_once_with(
        message, title="SQL error", severity="error", timeout=4, markup=False
    )
    return message


async def open_query(app: Parqx, pilot: Pilot[Any]) -> QueryWorkspace:
    await app.action_open_query()
    query = app.query_one("#results", TabbedContent).active_pane
    assert isinstance(query, QueryWorkspace)
    await wait_for(lambda: query.editor.has_focus, pilot)
    return query


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


async def toggle_sources(
    app: Parqx, pilot: Pilot[Any], *, expected_help: str | None = None
) -> None:
    await pilot.press("ctrl+p")
    palette = app.screen
    assert isinstance(palette, CommandPalette)
    palette.query_one(Input).value = "Sources"
    commands = palette.query_one(CommandList)
    await wait_for(lambda: commands.option_count == 1, pilot)
    if expected_help is not None:
        option = commands.get_option_at_index(0)
        assert isinstance(option, Command)
        assert option.hit.help == expected_help
    await pilot.press("down", "enter")
    await wait_for(lambda: app.screen is not palette, pilot)


async def run_query(app: Parqx, pilot: Pilot[Any], sql: str) -> ArrowTable:
    query = await open_query(app, pilot)
    query.editor.load_text(sql)
    await pilot.press("ctrl+enter")
    await wait_for(lambda: bool(query.query(ArrowTable)), pilot)
    table = query.query_one(ArrowTable)
    table.focus()
    await wait_for(lambda: table.has_focus, pilot)
    return table


async def select_tab(tabs: TabbedContent, pane_id: str, pilot: Pilot[Any]) -> None:
    """Switch through native input, including focus and activation messages."""
    assert await pilot.click(tabs.get_tab(pane_id))
    await wait_for(
        lambda: (
            tabs.active == pane_id
            and tabs.get_pane(pane_id).display
            and tabs.query_one(Tabs).has_focus
        ),
        pilot,
    )


async def confirm_query_close(app: Parqx, pilot: Pilot[Any]) -> None:
    """Explicitly confirm an already requested query-workspace discard."""
    await wait_for(lambda: app.screen.is_modal, pilot)
    close = app.screen.query_one("#close", Button)
    assert str(close.label) == "Close"
    assert await pilot.click(close)
    await wait_for(lambda: not app.screen.is_modal, pilot)
