"""Shared UI navigation and deterministic worker synchronization for tests."""

import asyncio
from collections.abc import Callable
from threading import Event
from types import TracebackType
from typing import Any, Self

from textual.command import Command, CommandList, CommandPalette
from textual.pilot import Pilot
from textual.widgets import Input


async def wait_for(
    predicate: Callable[[], bool], pilot: Pilot[Any], *, timeout: float = 5
) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await pilot.pause()


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


class WorkerGate:
    """Hold a worker at a known boundary and always release it on context exit."""

    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def pause(self) -> None:
        self.started.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("Test did not release the worker gate")

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release.set()
