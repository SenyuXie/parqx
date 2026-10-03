"""SQL result tabs and background request lifecycle."""

import asyncio
import gc
import weakref
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch

import pyarrow as pa
import pytest
from textual.coordinate import Coordinate
from textual.pilot import Pilot
from textual.widgets import Footer, Label, TabbedContent, Tabs
from textual.widgets._footer import FooterKey

from parqx.data.parquet import ParquetSource
from parqx.query.engine import QueryLimits, QueryPreview, QuerySession
from parqx.tui.app import ParqxApp
from parqx.tui.screens.query import QueryScreen
from parqx.tui.widgets import ArrowTable


async def wait_for(predicate: Callable[[], bool], pilot: Pilot[Any]) -> None:
    """Wait for an observable worker result while pumping the UI."""
    async with asyncio.timeout(5):
        while not predicate():
            await pilot.pause()


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
    """Switch through native input, including its focus and activation messages."""
    assert await pilot.click(tabs.get_tab(pane_id))
    await wait_for(
        lambda: (
            tabs.active == pane_id
            and tabs.get_pane(pane_id).display
            and tabs.query_one(Tabs).has_focus
        ),
        pilot,
    )


async def test_initial_file_tab_and_bounded_query_preview(small_parquet: Path) -> None:
    app = ParqxApp(small_parquet, query_limits=QueryLimits(preview_rows=2))
    async with app.run_test() as pilot:
        tabs = app.query_one("#results", TabbedContent)
        await wait_for(lambda: bool(tabs.query(ArrowTable)), pilot)
        await wait_for(lambda: not tabs.get_pane("source").loading, pilot)
        assert tabs.tab_count == 1
        assert tabs.active == "source"
        assert str(tabs.get_tab("source").label) == small_parquet.name
        source = tabs.get_pane("source").query_one(ArrowTable)
        assert (source.row_count, source.column_count) == (5, 3)

        table = await run_query(app, pilot, "SELECT name FROM smoke ORDER BY id")
        assert tabs.active == "query-1"
        assert str(tabs.get_tab("query-1").label) == "Query 1"
        assert (table.row_count, table.column_count) == (2, 1)
        assert table.columns[0].name == "name"
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == "alice"
        status = str(tabs.get_pane("query-1").query_one(Label).content)
        assert "2 rows" in status
        assert "preview" in status
        assert "total unknown" in status
        assert source.row_count == 5


async def test_query_error_preserves_tabs_and_empty_result_keeps_schema(
    small_parquet: Path,
) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        tabs = app.query_one(TabbedContent)
        table = await run_query(app, pilot, "SELECT id FROM smoke WHERE id > 3")
        query = await open_query(app, pilot)
        query.editor.load_text("SELECT missing FROM smoke")
        await pilot.press("enter")
        await wait_for(lambda: query.error is not None, pilot)
        await wait_for(lambda: query.editor.has_focus, pilot)
        assert app.screen is query
        assert not query.running
        assert tabs.tab_count == 2
        assert table.row_count == 2

        query.editor.load_text("SELECT name FROM smoke WHERE false")
        await pilot.press("enter")
        await wait_for(lambda: app.screen is not query and tabs.tab_count == 3, pilot)
        empty = tabs.get_pane("query-2").query_one(ArrowTable)
        assert empty.row_count == 0
        assert empty.columns[0].name == "name"
        assert query.error is None


async def test_tabs_preserve_independent_table_state(small_parquet: Path) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test(size=(60, 15)) as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        tabs = app.query_one(TabbedContent)
        first = await run_query(app, pilot, "SELECT i FROM range(100) t(i)")
        await pilot.press("h", "i", "z", "c")
        first.move_cursor(row=80, column=0, animate=False)
        await wait_for(lambda: first.scroll_y > 0, pilot)
        first_state = (
            first.show_header,
            first.show_row_index,
            first.zebra_stripes,
            first.cursor_type,
            first.cursor_coordinate,
            first.scroll_y,
        )
        second = await run_query(app, pilot, "SELECT 42 AS answer")
        assert second is not first
        assert second.show_header
        assert second.show_row_index
        assert not second.zebra_stripes
        assert second.cursor_type == "cell"
        await select_tab(tabs, "query-1", pilot)
        assert (
            first.show_header,
            first.show_row_index,
            first.zebra_stripes,
            first.cursor_type,
            first.cursor_coordinate,
            first.scroll_y,
        ) == first_state


async def test_close_tab_footer_and_last_tab_guard(small_parquet: Path) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        tabs = app.query_one(TabbedContent)
        footer = app.query_one(Footer)

        def close_keys() -> list[FooterKey]:
            return [key for key in footer.query(FooterKey) if key.action == "close_tab"]

        await wait_for(lambda: bool(close_keys()), pilot)
        assert close_keys()[0].description == "Close tab"
        assert close_keys()[0].key == "ctrl+w"
        assert close_keys()[0].has_class("-disabled")
        await pilot.press("ctrl+w")
        assert tabs.tab_count == 1
        await run_query(app, pilot, "SELECT 42 AS answer")
        await wait_for(
            lambda: bool(close_keys()) and not close_keys()[0].has_class("-disabled"),
            pilot,
        )
        await select_tab(tabs, "source", pilot)
        await pilot.press("ctrl+w")
        await wait_for(lambda: tabs.tab_count == 1 and tabs.active == "query-1", pilot)
        assert not tabs.query("#source")
        await wait_for(
            lambda: bool(close_keys()) and close_keys()[0].has_class("-disabled"), pilot
        )
        assert app.check_action("close_tab", ()) is None
        await pilot.press("ctrl+w", "ctrl+w", "ctrl+w")
        assert tabs.tab_count == 1
        assert tabs.active == "query-1"

        # Closing the source tab does not remove the engine's file-backed view.
        await run_query(app, pilot, "SELECT count(*) AS total FROM smoke")
        await run_query(app, pilot, "SELECT 3 AS value")
        await pilot.press("ctrl+w", "ctrl+w", "ctrl+w", "ctrl+w")
        await wait_for(lambda: tabs.tab_count == 1, pilot)
        assert tabs.active


@pytest.mark.parametrize("stale_error", [False, True])
async def test_cancelled_query_cannot_publish_into_reopened_dialog(
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

    app = ParqxApp(small_parquet)
    with patch.object(QuerySession, "preview", delayed_preview):
        async with app.run_test() as pilot:
            try:
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                tabs = app.query_one(TabbedContent)
                query = await open_query(app, pilot)
                query.editor.load_text("SELECT 1 AS obsolete")
                await pilot.press("enter")
                await wait_for(started.is_set, pilot)
                control = query._query_control  # pyright: ignore[reportPrivateUsage]
                assert control is not None
                await pilot.press("escape")
                await wait_for(lambda: app.screen is not query, pilot)
                assert control.cancelled.is_set()
                assert tabs.tab_count == 1
                table = await run_query(app, pilot, "SELECT 42 AS answer")
                await open_query(app, pilot)
                release.set()
                await wait_for(control.finished.is_set, pilot)
                await query.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                assert app.screen is query
                assert query.error is None
                assert not query.running
                assert tabs.tab_count == 2
                assert table.get_cell_at(Coordinate(0, 0)).as_py() == 42
            finally:
                release.set()


@pytest.mark.parametrize("stale_error", [False, True])
async def test_file_read_cannot_recreate_closed_source_tab(
    small_parquet: Path, stale_error: bool
) -> None:
    started, release, returned = Event(), Event(), Event()

    def delayed_read(path: Path) -> ParquetSource:
        started.set()
        release.wait(timeout=5)
        try:
            if stale_error:
                raise OSError("obsolete metadata read failed")
            return ParquetSource(path)
        finally:
            returned.set()

    app = ParqxApp(small_parquet)
    with patch("parqx.tui.app.ParquetSource", delayed_read):
        async with app.run_test() as pilot:
            try:
                await wait_for(started.is_set, pilot)
                tabs = app.query_one(TabbedContent)
                source = tabs.get_pane("source")
                assert source.loading
                assert not source.query(ArrowTable)
                table = await run_query(app, pilot, "SELECT 42 AS answer")
                await select_tab(tabs, "source", pilot)
                await pilot.press("ctrl+w")
                await wait_for(lambda: not tabs.query("#source"), pilot)
                release.set()
                await wait_for(returned.is_set, pilot)
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                assert tabs.tab_count == 1
                assert tabs.active == "query-1"
                assert table.columns[0].name == "answer"
                assert app.load_error is None
                if not stale_error:
                    count = await run_query(app, pilot, "SELECT count(*) FROM smoke")
                    assert count.get_cell_at(Coordinate(0, 0)).as_py() == 5
            finally:
                release.set()


@pytest.mark.parametrize("cancel_explicitly", [False, True])
async def test_query_cancellation_and_shutdown_finish_worker(
    small_parquet: Path, cancel_explicitly: bool
) -> None:
    started = Event()
    original_enter = QuerySession.__enter__

    def wait_for_cancel(session: QuerySession) -> QuerySession:
        started.set()
        session.control.cancelled.wait(timeout=5)
        return original_enter(session)

    app = ParqxApp(small_parquet)
    with patch.object(QuerySession, "__enter__", wait_for_cancel):
        async with app.run_test() as pilot:
            await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
            tabs = app.query_one(TabbedContent)
            query = await open_query(app, pilot)
            query.editor.load_text("SELECT 42")
            await pilot.press("enter")
            await wait_for(started.is_set, pilot)
            control = query._query_control  # pyright: ignore[reportPrivateUsage]
            assert control is not None
            if cancel_explicitly:
                await pilot.press("escape")
                await wait_for(lambda: app.screen is not query, pilot)
                await wait_for(control.finished.is_set, pilot)
                assert tabs.tab_count == 1
                assert not query.running
                assert query.error is None
    assert control.cancelled.is_set()
    assert control.finished.is_set()


@pytest.mark.parametrize("pane_id", ["source", "query-1"])
async def test_closing_tab_releases_its_cached_data(
    small_parquet: Path, pane_id: str
) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        tabs = app.query_one(TabbedContent)
        await run_query(app, pilot, "SELECT 42 AS answer")
        await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
        data = weakref.ref(tabs.get_pane(pane_id).query_one(ArrowTable).data)
        await select_tab(tabs, pane_id, pilot)
        await pilot.press("ctrl+w")
        await wait_for(lambda: not tabs.query(f"#{pane_id}"), pilot)

        def released() -> bool:
            gc.collect()
            return data() is None

        await wait_for(released, pilot)
        assert tabs.tab_count == 1


async def test_filename_sql_replaces_implicit_data_alias(small_parquet: Path) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        query = await open_query(app, pilot)
        assert '"smoke"' in str(query.query_one("#query-sources", Label).content)
        query.editor.load_text("SELECT * FROM data")
        await pilot.press("enter")
        await wait_for(lambda: query.error is not None, pilot)
        assert "data" in (query.error or "")
        query.editor.load_text('SELECT count(*) FROM "smoke"')
        await pilot.press("enter")
        await wait_for(lambda: app.screen is not query, pilot)


async def test_unavailable_source_warning_and_error_recover(
    small_parquet: Path,
) -> None:
    contents = small_parquet.read_bytes()
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
        small_parquet.unlink()
        await run_query(app, pilot, "SELECT 42 AS answer")
        tabs = app.query_one(TabbedContent)
        assert "warning" in str(tabs.get_pane("query-1").query_one(Label).content)
        tooltip = str(tabs.get_tab("query-1").tooltip)
        assert str(small_parquet) in tooltip
        assert '"smoke"' in tooltip
        query = await open_query(app, pilot)
        query.editor.load_text("SELECT * FROM smoke")
        await pilot.press("enter")
        await wait_for(lambda: query.error is not None, pilot)
        assert "Unavailable sources" in (query.error or "")
        assert str(small_parquet) in (query.error or "")
        small_parquet.write_bytes(contents)
        query.editor.load_text("SELECT count(*) FROM smoke")
        await pilot.press("enter")
        await wait_for(lambda: app.screen is not query and tabs.tab_count == 3, pilot)
        assert "warning" not in str(tabs.get_pane("query-2").query_one(Label).content)


async def test_query_freezes_sources_before_worker_starts(small_parquet: Path) -> None:
    started, release = Event(), Event()
    original_enter = QuerySession.__enter__

    def delayed_enter(session: QuerySession) -> QuerySession:
        started.set()
        release.wait(timeout=5)
        return original_enter(session)

    app = ParqxApp(small_parquet)
    with patch.object(QuerySession, "__enter__", delayed_enter):
        async with app.run_test() as pilot:
            try:
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                query = await open_query(app, pilot)
                query.editor.load_text("SELECT count(*) FROM smoke")
                await pilot.press("enter")
                await wait_for(started.is_set, pilot)
                app.catalog.mark_failed("source-1", "Temporarily unavailable")
                release.set()
                await wait_for(lambda: app.screen is not query, pilot)
                tabs = app.query_one(TabbedContent)
                table = tabs.get_pane("query-1").query_one(ArrowTable)
                assert table.get_cell_at(Coordinate(0, 0)).as_py() == 5
                await open_query(app, pilot)
                await pilot.press("enter")
                await wait_for(lambda: query.error is not None, pilot)
                assert "Temporarily unavailable" in (query.error or "")
            finally:
                release.set()
