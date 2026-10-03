"""Shared UI actions; wait for observable state instead of fixed delays."""

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import Mock

from textual.pilot import Pilot
from textual.widgets import Footer, TabbedContent, Tabs
from textual.widgets._footer import FooterKey

from parqx.tui.app import ParqxApp
from parqx.tui.screens.query import QueryScreen
from parqx.tui.widgets import ArrowTable


async def wait_for(predicate: Callable[[], bool], pilot: Pilot[Any]) -> None:
    """Wait for an observable worker result while pumping the UI."""
    async with asyncio.timeout(5):
        while not predicate():
            await pilot.pause()


async def wait_for_query_error(
    notify: Mock, query: QueryScreen, pilot: Pilot[Any]
) -> str:
    await wait_for(lambda: notify.called and not query.running, pilot)
    assert notify.call_args is not None
    message = notify.call_args.args[0]
    assert isinstance(message, str)
    notify.assert_called_once_with(
        message, title="SQL error", severity="error", timeout=4, markup=False
    )
    return message


async def open_query(app: ParqxApp, pilot: Pilot[Any]) -> QueryScreen:
    app.action_open_query()
    query = app.get_screen("query", QueryScreen)  # pyright: ignore[reportUnknownMemberType]
    await wait_for(lambda: app.screen is query and query.editor.has_focus, pilot)
    return query


async def run_query(app: ParqxApp, pilot: Pilot[Any], sql: str) -> ArrowTable:
    tabs = app.query_one("#results", TabbedContent)
    count = tabs.tab_count
    previous_active = tabs.active
    query = await open_query(app, pilot)
    query.editor.load_text(sql)
    await pilot.press("enter")
    await wait_for(
        lambda: (
            app.screen is not query
            and tabs.tab_count == count + 1
            and tabs.active != previous_active
        ),
        pilot,
    )
    table = tabs.active_pane.query_one(ArrowTable) if tabs.active_pane else None
    assert table is not None
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


def footer_keys(footer: Footer) -> dict[str, FooterKey]:
    return {key.action: key for key in footer.query(FooterKey)}


def footer_ready(footer: Footer) -> bool:
    keys = footer_keys(footer)
    return len(keys) == 3 and all(key.region.width > 0 for key in keys.values())
