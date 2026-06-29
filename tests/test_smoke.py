"""End-to-end smoke tests for the Parqx TUI."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from textual.pilot import Pilot

from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable


async def _wait_until(
    predicate: Callable[[], bool], pilot: Pilot[Any], *, timeout: float = 2.0
) -> None:
    """Pump the message loop until `predicate()` is truthy or `timeout` elapses."""

    async def _loop() -> None:
        while not predicate():
            await pilot.pause()

    await asyncio.wait_for(_loop(), timeout=timeout)


async def test_app_loads_parquet_and_navigates(small_parquet: Path) -> None:
    """Boot the app, wait for async load, drive the cursor, quit cleanly."""
    app = ParqxApp(path=small_parquet)
    async with app.run_test() as pilot:
        # Loader runs in a @work(thread=True) worker; wait for it to swap
        # FileLoading for ArrowTable in the DOM.
        await _wait_until(lambda: bool(app.query(ArrowTable)), pilot)

        table = app.query_one(ArrowTable)
        assert table.row_count == 5
        assert table.column_count == 3

        await pilot.press("down", "down", "right")
        assert table.cursor_coordinate.row == 2
        assert table.cursor_coordinate.column == 1

    assert app.load_error is None


async def test_app_reports_load_error_for_corrupt_file(tmp_path: Path) -> None:
    """A non-parquet file surfaces `load_error` instead of crashing."""
    bogus = tmp_path / "not_a_parquet.txt"
    bogus.write_text("definitely not parquet")

    app = ParqxApp(path=bogus)
    async with app.run_test() as pilot:
        # _on_load_error sets `load_error` and then calls self.exit(), which
        # marks the worker CANCELLED — so we can't await the worker. Poll the
        # observable contract (`load_error`) instead.
        await _wait_until(lambda: app.load_error is not None, pilot)

    assert app.load_error is not None
