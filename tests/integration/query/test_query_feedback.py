"""Current-result transitions through the mounted query workspace."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from unittest.mock import patch

import duckdb
import pytest
from textual.coordinate import Coordinate
from textual.widgets import Static, TabbedContent

from parqx.config import ParqxConfig
from parqx.tui.app import Parqx
from parqx.tui.table.arrow_table import ArrowTable
from tests.helpers import (
    confirm_query_close,
    open_query,
    press_run_button,
    select_tab,
    wait_for,
    wait_for_query_error,
)


@contextmanager
def paused_sql() -> Generator[tuple[Event, Event], None, None]:
    started, release = Event(), Event()
    original_sql = duckdb.DuckDBPyConnection.sql

    def delayed_sql(
        connection: duckdb.DuckDBPyConnection, sql: str
    ) -> duckdb.DuckDBPyRelation:
        started.set()
        assert release.wait(timeout=15)
        return original_sql(connection, sql)

    with patch.object(duckdb.DuckDBPyConnection, "sql", delayed_sql):
        try:
            yield started, release
        finally:
            release.set()


@pytest.mark.parametrize(
    ("config", "sql", "message"),
    [
        (
            ParqxConfig(preview_rows=2),
            "SELECT * FROM range(3)",
            "Showing 2 rows (2-row preview limit).",
        ),
        (
            ParqxConfig(preview_bytes=1024),
            "SELECT repeat('x', 1024) FROM range(3)",
            "Showing 1 row (1 KiB preview limit).",
        ),
    ],
)
async def test_truncated_preview_shows_the_row_count_and_limit(
    small_parquet: Path, config: ParqxConfig, sql: str, message: str
) -> None:
    app = Parqx([small_parquet], config=config)
    async with app.run_test(notifications=True) as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        query = await open_query(app, pilot)
        query.editor.load_text(sql)
        await press_run_button(query, pilot)
        await wait_for(lambda: query.table is not None and not query.running, pilot)
        await wait_for(lambda: bool(app.query("Toast")), pilot)
        toast = app.query_one("Toast.-information", Static)
        assert str(toast.render()) == f"Query 1 · Partial result\n{message}"


async def test_background_preview_notifies_once_when_its_tab_is_viewed(
    small_parquet: Path,
) -> None:
    app = Parqx([small_parquet], config=ParqxConfig(preview_rows=2))
    async with app.run_test(notifications=True) as pilot:
        tabs = app.query_one(TabbedContent)
        query = await open_query(app, pilot)
        query.editor.load_text("SELECT * FROM range(3)")
        await wait_for(lambda: not query.run_button.disabled, pilot)
        query.run_button.press()
        tabs.active = "source-1"
        await wait_for(lambda: query.current_result is not None, pilot)
        await pilot.pause()
        assert not app.query("Toast")

        await select_tab(tabs, "query-1", pilot)
        await wait_for(lambda: bool(app.query("Toast")), pilot)
        toast = app.query_one("Toast.-information", Static)
        assert "Query 1 · Partial result" in str(toast.render())
        assert await pilot.click(toast)
        await select_tab(tabs, "source-1", pilot)
        await select_tab(tabs, "query-1", pilot)
        assert not app.query("Toast")


async def test_preview_finishing_under_the_command_palette_waits_for_dismissal(
    small_parquet: Path,
) -> None:
    app = Parqx([small_parquet], config=ParqxConfig(preview_rows=1))
    async with app.run_test(notifications=True) as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        query = await open_query(app, pilot)
        query.editor.load_text("SELECT 1")
        await press_run_button(query, pilot)
        await wait_for(lambda: query.table is not None and not query.running, pilot)
        query.editor.load_text("SELECT * FROM range(3)")
        with (
            paused_sql() as (started, release),
            patch.object(app, "notify", wraps=app.notify) as notify,
        ):
            await press_run_button(query, pilot)
            await wait_for(started.is_set, pilot)
            await pilot.press("ctrl+p")
            assert app.screen.is_modal
            release.set()
            await wait_for(lambda: not query.running, pilot)
            await pilot.pause()
            notify.assert_not_called()
        await pilot.press("escape")
        await wait_for(lambda: bool(app.query("Toast.-information")), pilot)


@pytest.mark.parametrize("action", ["restore", "replace", "close"])
async def test_only_the_current_preview_notifies_after_restoring_the_table(
    small_parquet: Path, action: str
) -> None:
    app = Parqx([small_parquet], config=ParqxConfig(preview_rows=2))
    async with app.run_test(notifications=True) as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        query = await open_query(app, pilot)
        app.screen.action_maximize()
        await wait_for(lambda: query.editor.is_maximized, pilot)
        query.editor.load_text("SELECT * FROM range(3)")
        await press_run_button(query, pilot)
        await wait_for(lambda: query.table is not None and not query.running, pilot)
        assert not app.query("Toast")

        if action == "close":
            await pilot.press("ctrl+w")
            await confirm_query_close(app, pilot)
            await wait_for(lambda: not query.is_attached, pilot)
        else:
            if action == "replace":
                # This complete result exactly matches the row budget.
                query.editor.load_text("SELECT * FROM range(2)")
                await press_run_button(query, pilot)
                await wait_for(
                    lambda: (
                        query.current_result is not None
                        and query.current_result.sql == "SELECT * FROM range(2)"
                    ),
                    pilot,
                )
            await pilot.press("escape")
            await wait_for(lambda: app.screen.maximized is None, pilot)
        if action == "restore":
            await wait_for(lambda: bool(app.query("Toast.-information")), pilot)
        else:
            assert not app.query("Toast")


async def test_retry_retains_previous_success_then_replaces_it_even_with_zero_rows(
    small_parquet: Path,
) -> None:
    import gc
    import weakref

    started, release = Event(), Event()
    original_sql = duckdb.DuckDBPyConnection.sql
    failing_sql = 'SELECT "missing [bold]汉字[/bold]" FROM smoke'

    def delayed_sql(
        connection: duckdb.DuckDBPyConnection, sql: str
    ) -> duckdb.DuckDBPyRelation:
        if sql == failing_sql:
            started.set()
            assert release.wait(timeout=15)
        return original_sql(connection, sql)

    app = Parqx([small_parquet])
    with (
        patch.object(duckdb.DuckDBPyConnection, "sql", delayed_sql),
        patch.object(app, "notify") as notify,
    ):
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                tabs = app.query_one(TabbedContent)
                await wait_for(lambda: not tabs.get_pane("source-1").loading, pilot)
                query = await open_query(app, pilot)
                first_sql = "SELECT name FROM smoke ORDER BY id"
                query.editor.load_text(first_sql)
                await press_run_button(query, pilot)
                await wait_for(
                    lambda: bool(query.query(ArrowTable)) and not query.running, pilot
                )
                table = query.query_one(ArrowTable)
                first_data = weakref.ref(table.data)
                query.editor.load_text(failing_sql)
                await pilot.pause()
                assert table.data is first_data()
                assert table.columns[0].name == "name"
                await press_run_button(query, pilot)
                await wait_for(started.is_set, pilot)
                assert query.run_button.disabled
                assert table.get_cell_at(Coordinate(0, 0)).as_py() == "alice"
                assert table.row_count == 5
                release.set()
                message = await wait_for_query_error(notify, query, pilot)
                assert "missing [bold]汉字[/bold]" in message
                query.editor.load_text(first_sql)
                await pilot.pause()
                assert table.row_count == 5
                query.editor.focus()
                query.editor.load_text("SELECT 42 AS recovered")
                await press_run_button(query, pilot)
                await wait_for(
                    lambda: table.columns[0].name == "recovered" and not query.running,
                    pilot,
                )
                assert table.get_cell_at(Coordinate(0, 0)).as_py() == 42

                def released() -> bool:
                    gc.collect()
                    return first_data() is None

                await wait_for(released, pilot)
                query.editor.load_text("SELECT id AS empty_id FROM smoke WHERE false")
                await press_run_button(query, pilot)
                await wait_for(
                    lambda: table.row_count == 0 and not query.running, pilot
                )
                assert table.columns[0].name == "empty_id"
                assert tabs.tab_count == 2
            finally:
                release.set()


async def test_failure_notifies_and_allows_retry(small_parquet: Path) -> None:
    app = Parqx([small_parquet])
    async with app.run_test(size=(80, 24)) as pilot:
        with paused_sql() as (started, release), patch.object(app, "notify") as notify:
            await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
            query = await open_query(app, pilot)
            query.editor.load_text("SELECT * FROM missing_table")
            await press_run_button(query, pilot)
            await wait_for(started.is_set, pilot)
            assert query.run_button.disabled
            assert not query.query(ArrowTable)
            release.set()
            message = await wait_for_query_error(notify, query, pilot)
            assert "missing_table" in message
            assert not query.run_button.disabled
            assert not query.query(ArrowTable)
            query.editor.load_text("SELECT 42 AS recovered")
            await pilot.pause()
            started.clear()
            release.clear()
            query.editor.focus()
            await press_run_button(query, pilot)
            await wait_for(started.is_set, pilot)
            assert query.run_button.disabled
            assert not query.query(ArrowTable)
            release.set()
            await wait_for(
                lambda: bool(query.query(ArrowTable)) and not query.running, pilot
            )
            assert not query.run_button.disabled
            assert notify.call_count == 1
            assert (
                query.query_one(ArrowTable).get_cell_at(Coordinate(0, 0)).as_py() == 42
            )
