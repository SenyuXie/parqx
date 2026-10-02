"""SQL previews, result replacement, and background request lifecycle."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from textual.coordinate import Coordinate
from textual.pilot import Pilot
from textual.widgets import TextArea

from parqx.query.engine import QueryLimits, QueryPreview, QuerySession
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable, FileLoading


async def wait_for(predicate: Callable[[], bool], pilot: Pilot[Any]) -> None:
    """Wait for an observable worker result while pumping the UI."""
    async with asyncio.timeout(5):
        while not predicate():
            await pilot.pause()


async def test_initial_sql_bypasses_full_read_and_bounds_preview(
    small_parquet: Path,
) -> None:
    app = ParqxApp(
        small_parquet,
        initial_sql="SELECT name FROM data ORDER BY id",
        query_limits=QueryLimits(preview_rows=2),
    )
    with patch("pyarrow.parquet.read_table", side_effect=AssertionError("full read")):
        async with app.run_test() as pilot:
            await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
            table = app.query_one(ArrowTable)
            assert (table.row_count, table.column_count) == (2, 1)
            assert table.columns[0].name == "name"
            assert table.get_cell_at(Coordinate(0, 0)).as_py() == "alice"
            assert app.query_error is None
            assert not app.query_running
            assert not app.query(FileLoading)


@pytest.mark.parametrize("focus_sql", [False, True])
async def test_query_replace_error_empty_and_browse(
    small_parquet: Path, focus_sql: bool
) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        app.action_toggle_query()
        await pilot.pause()
        editor = app.query_one(TextArea)
        table = app.query_one(ArrowTable)
        target = editor if focus_sql else table
        editor.load_text("SELECT id FROM data WHERE id > 3")
        target.focus()
        await pilot.press("f1")
        await wait_for(lambda: not app.query_running, pilot)
        assert (table.row_count, table.column_count) == (2, 1)
        assert table.has_focus

        editor.load_text("SELECT missing FROM data")
        target.focus()
        await pilot.press("f1")
        await wait_for(lambda: app.query_error is not None, pilot)
        assert table.row_count == 2
        assert editor.has_focus

        editor.load_text("SELECT name FROM data WHERE false")
        target.focus()
        await pilot.press("f1")
        await wait_for(lambda: not app.query_running, pilot)
        assert table.row_count == 0
        assert table.columns[0].name == "name"
        assert app.query_error is None

        target.focus()
        await pilot.press("f3")
        await wait_for(lambda: table.row_count == 5, pilot)
        assert table.column_count == 3
        assert table.has_focus


async def test_initial_query_error_replaces_loader_with_empty_table(
    small_parquet: Path,
) -> None:
    app = ParqxApp(small_parquet, initial_sql="SELECT missing FROM data")
    async with app.run_test() as pilot:
        await wait_for(lambda: app.query_error is not None, pilot)
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        assert app.query_one(ArrowTable).row_count == 0
        assert not app.query(FileLoading)
        assert not app.query_running
        assert app.query_one(TextArea).has_focus


@pytest.mark.parametrize("stale_error", [False, True])
async def test_superseded_query_cannot_replace_new_result(
    small_parquet: Path, stale_error: bool
) -> None:
    started, release = Event(), Event()
    original_preview = QuerySession.preview

    def delayed_preview(session: QuerySession) -> QueryPreview:
        if session.sql == "SELECT 1 AS obsolete":
            started.set()
            release.wait(timeout=5)
            if stale_error:
                raise ValueError("obsolete query failed")
            return QueryPreview(pa.table({"obsolete": [1]}), truncated=False)
        return original_preview(session)

    app = ParqxApp(small_parquet, initial_sql="SELECT 1 AS obsolete")
    with patch.object(QuerySession, "preview", delayed_preview):
        async with app.run_test() as pilot:
            try:
                await wait_for(started.is_set, pilot)
                control = app._query_control  # pyright: ignore[reportPrivateUsage]
                assert control is not None
                app.query_one(TextArea).load_text("SELECT 42 AS answer")
                await pilot.press("f1")
                await wait_for(lambda: not app.query_running, pilot)
                table = app.query_one(ArrowTable)
                assert table.columns[0].name == "answer"
                release.set()
                await wait_for(control.finished.is_set, pilot)
                assert control.cancelled.is_set()
                assert app.query_error is None
                assert table.get_cell_at(Coordinate(0, 0)).as_py() == 42
                assert table.columns[0].name == "answer"
            finally:
                release.set()


async def test_superseded_file_read_cannot_replace_query_result(
    small_parquet: Path,
) -> None:
    started, release, returned = Event(), Event(), Event()
    original_read = pq.read_table

    def delayed_read(path: Path) -> pa.Table:
        started.set()
        release.wait(timeout=5)
        result = original_read(path)
        returned.set()
        return result

    app = ParqxApp(small_parquet)
    with patch("parqx.tui.app.pq.read_table", delayed_read):
        async with app.run_test() as pilot:
            try:
                await wait_for(started.is_set, pilot)
                app.action_toggle_query()
                app.query_one(TextArea).load_text("SELECT 42 AS answer")
                await pilot.press("f1")
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                table = app.query_one(ArrowTable)
                assert table.columns[0].name == "answer"
                release.set()
                await wait_for(returned.is_set, pilot)
                await pilot.pause()
                assert table.row_count == 1
                assert table.columns[0].name == "answer"
                assert app.load_error is None
            finally:
                release.set()


@pytest.mark.parametrize("cancel_explicitly", [False, True])
async def test_initial_query_cancellation_and_shutdown_finish_worker(
    small_parquet: Path, cancel_explicitly: bool
) -> None:
    started = Event()
    original_enter = QuerySession.__enter__

    def wait_for_cancel(session: QuerySession) -> QuerySession:
        started.set()
        session.control.cancelled.wait(timeout=5)
        return original_enter(session)

    app = ParqxApp(small_parquet, initial_sql="SELECT 42")
    with patch.object(QuerySession, "__enter__", wait_for_cancel):
        async with app.run_test() as pilot:
            await wait_for(started.is_set, pilot)
            control = app._query_control  # pyright: ignore[reportPrivateUsage]
            assert control is not None
            if cancel_explicitly:
                await pilot.press("f2")
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                assert app.query_one(ArrowTable).row_count == 0
                assert not app.query(FileLoading)
                assert not app.query_running
                assert app.query_error is None
    assert control.cancelled.is_set()
    assert control.finished.is_set()
