"""Query input ownership, worker delivery and natural native cleanup."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
from unittest.mock import patch

import duckdb
import pytest
from textual.coordinate import Coordinate
from textual.message import Message
from textual.widgets import TabbedContent
from textual.worker import Worker, WorkerState

from parqx.config import ParqxConfig
from parqx.data.parquet import ParquetSource
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from tests.helpers import (
    open_query,
    run_query,
    select_tab,
    wait_for,
    wait_for_query_error,
)


async def test_running_query_blocks_edits_and_duplicate_execution(
    small_parquet: Path,
) -> None:
    started, release = Event(), Event()
    original_sql = duckdb.DuckDBPyConnection.sql
    calls = 0

    def delayed_sql(
        connection: duckdb.DuckDBPyConnection, sql: str
    ) -> duckdb.DuckDBPyRelation:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=15)
        return original_sql(connection, sql)

    app = ParqxApp([small_parquet])
    with patch.object(duckdb.DuckDBPyConnection, "sql", delayed_sql):
        async with app.run_test() as pilot:
            try:
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                tabs = app.query_one(TabbedContent)
                query = await open_query(app, pilot)
                query.editor.load_text("SELECT 42")
                await pilot.press("ctrl+enter")
                await wait_for(started.is_set, pilot)
                assert query.run_button.disabled
                with patch.object(app, "notify", wraps=app.notify) as notify:
                    await pilot.press("ctrl+enter", "enter", "x", "backspace", "ctrl+w")
                    assert "busy" in str(notify.call_args).lower()
                    assert query.editor.text == "SELECT 42"
                    assert query.editor.read_only
                    second = await open_query(app, pilot)
                    second.editor.load_text("SELECT 17")
                    await wait_for(lambda: not second.run_button.disabled, pilot)
                    assert await pilot.click(second.run_button)
                    assert not second.editor.read_only
                    assert calls == 1
                    await select_tab(tabs, "source-1", pilot)
                    source = tabs.get_pane("source-1").query_one(ArrowTable)
                    source.focus()
                    await pilot.press("down", "ctrl+enter")
                    assert source.cursor_coordinate.row == 1
                    assert calls == 1
                    await pilot.press("ctrl+w")
                    await wait_for(lambda: not tabs.query("#source-1"), pilot)
                    await select_tab(tabs, "query-2", pilot)
                    second.editor.focus()
                    release.set()
                    await wait_for(lambda: not query.running, pilot)
                    assert (
                        query.query_one(ArrowTable)
                        .get_cell_at(Coordinate(0, 0))
                        .as_py()
                        == 42
                    )
                    assert second.editor.has_focus
                    assert tabs.active == "query-2"
                    assert calls == 1
                    assert not second.query(ArrowTable)
                    assert not query.run_button.disabled
            finally:
                release.set()


async def test_metadata_completion_cannot_recreate_closed_source_tabs(
    small_parquet: Path,
) -> None:
    failed = small_parquet.with_name("failed.parquet")
    healthy = small_parquet.with_name("healthy.parquet")
    for path in (failed, healthy):
        path.write_bytes(small_parquet.read_bytes())
    started = {path: Event() for path in (small_parquet, failed)}
    release = Event()

    def delayed_read(path: Path, config: ParqxConfig) -> ParquetSource:
        if path == healthy:
            return ParquetSource(
                path,
                page_rows=4096,
                page_bytes=4 * 1024 * 1024,
                memory_limit="256MB",
                threads=2,
            )
        started[path].set()
        assert release.wait(timeout=10)
        if path == failed:
            raise OSError("obsolete metadata read failed")
        return ParquetSource(
            path,
            page_rows=4096,
            page_bytes=4 * 1024 * 1024,
            memory_limit="256MB",
            threads=2,
        )

    app = ParqxApp([small_parquet, failed, healthy])
    with patch("parqx.tui.app._open_source", delayed_read):
        async with app.run_test() as pilot:
            try:
                await wait_for(
                    lambda: all(event.is_set() for event in started.values()), pilot
                )
                tabs = app.query_one(TabbedContent)
                assert tabs.get_pane("source-1").loading
                assert tabs.get_pane("source-2").loading
                # Both callbacks arrive after their tabs have gone away.
                await select_tab(tabs, "source-1", pilot)
                await pilot.press("ctrl+w")
                await wait_for(lambda: not tabs.query("#source-1"), pilot)
                await select_tab(tabs, "source-2", pilot)
                await pilot.press("ctrl+w")
                await wait_for(lambda: not tabs.query("#source-2"), pilot)
                release.set()
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                assert tabs.tab_count == 1
                assert tabs.active == "source-3"
                assert len(app.load_errors) == 1
                assert "obsolete metadata read failed" in str(app.load_errors)
                # Successful metadata still registers the source for SQL.
                table = await run_query(app, pilot, "SELECT count(*) FROM smoke")
                assert table.get_cell_at(Coordinate(0, 0)).as_py() == 5
            finally:
                release.set()


@pytest.mark.parametrize(
    "failure", [RuntimeError("unexpected execution failure"), MemoryError()]
)
async def test_native_failure_unlocks_editor_and_allows_another_query(
    small_parquet: Path, failure: Exception
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        query = await open_query(app, pilot)
        query.editor.load_text("SELECT 42")
        with (
            patch.object(duckdb.DuckDBPyConnection, "sql", side_effect=failure),
            patch.object(app, "notify", wraps=app.notify) as notify,
        ):
            await pilot.press("ctrl+enter")
            message = await wait_for_query_error(notify, query, pilot)
            assert (str(failure) or type(failure).__name__) in message
            assert not query.editor.read_only
            assert query.editor.has_focus
        result = await run_query(app, pilot, "SELECT 42 AS answer")
        assert result.get_cell_at(Coordinate(0, 0)).as_py() == 42


@pytest.mark.parametrize("fail", [False, True])
async def test_quit_leaves_native_work_to_finish_and_cleanup(
    small_parquet: Path, tmp_path: Path, fail: bool
) -> None:
    started, release, closed = Event(), Event(), Event()
    directories: list[Path] = []
    query_thread: list[int] = []
    original_sql = duckdb.DuckDBPyConnection.sql
    original_close = duckdb.DuckDBPyConnection.close

    def temporary(*, prefix: str) -> TemporaryDirectory[str]:
        directory = TemporaryDirectory(prefix=prefix, dir=tmp_path)
        directories.append(Path(directory.name))
        return directory

    def delayed_sql(
        connection: duckdb.DuckDBPyConnection, sql: str
    ) -> duckdb.DuckDBPyRelation:
        query_thread.append(get_ident())
        started.set()
        assert release.wait(timeout=10)
        if fail:
            raise OSError("late query error")
        return original_sql(connection, sql)

    def close(connection: duckdb.DuckDBPyConnection) -> None:
        original_close(connection)
        if get_ident() in query_thread:
            closed.set()

    app = ParqxApp([small_parquet])
    with (
        patch.object(duckdb.DuckDBPyConnection, "sql", delayed_sql),
        patch.object(duckdb.DuckDBPyConnection, "close", close),
        patch("parqx.data.duckdb.TemporaryDirectory", temporary),
        patch.object(app, "notify", wraps=app.notify) as notify,
    ):
        try:
            async with app.run_test() as pilot:
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                query = await open_query(app, pilot)
                query.editor.load_text("SELECT 42")
                await pilot.press("ctrl+enter")
                await wait_for(started.is_set, pilot)
            assert not closed.is_set()
            release.set()
            assert closed.wait(timeout=5)
        finally:
            release.set()
        # The connection closes just before its temporary directory. Join the
        # executor at the test boundary before checking all native cleanup.

        await asyncio.get_running_loop().shutdown_default_executor()
        assert all(not directory.exists() for directory in directories)
        notify.assert_not_called()


async def test_completed_worker_keeps_workspace_locked_until_message_delivery(
    small_parquet: Path,
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        query = await open_query(app, pilot)
        query.editor.load_text("SELECT 42")
        held: list[Message] = []
        post_message = app.post_message

        def hold_completion(message: Message) -> bool:
            if (
                isinstance(message, Worker.StateChanged)
                and message.state == WorkerState.SUCCESS
            ):
                held.append(message)
                return True
            return post_message(message)

        with patch.object(app, "post_message", hold_completion):
            await pilot.press("ctrl+enter")
            await wait_for(lambda: bool(held), pilot)
            await pilot.press("ctrl+w")
            await pilot.press("ctrl+enter")
            assert query.running
            assert app.query_one(TabbedContent).active_pane is query
            assert query.editor.read_only
            assert len(held) == 1
            post_message(held[0])
            await wait_for(
                lambda: bool(query.query(ArrowTable)) and not query.running, pilot
            )
            assert app.query_one(TabbedContent).tab_count == 2
