"""Resume work through the mounted application's empty-state actions."""

from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch

import duckdb
import pytest
from textual.coordinate import Coordinate
from textual.widgets import Button, Static, TabbedContent

from parqx.data.view import PageCache
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from parqx.tui.widgets.query_workspace import QueryWorkspace
from parqx.tui.widgets.sources_tree import SourcesTree
from tests.helpers import (
    confirm_query_close,
    open_query,
    open_sql_query,
    run_query,
    select_tab,
    wait_for,
)


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_last_file_closes_and_query_action_resumes_with_retained_sources(
    small_parquet: Path, size: tuple[int, int]
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test(size=size) as pilot:
        tabs = app.query_one(TabbedContent)
        await wait_for(lambda: bool(tabs.query(ArrowTable)), pilot)
        await pilot.press("ctrl+w")
        await wait_for(lambda: tabs.tab_count == 0, pilot)
        assert not app.screen.is_modal
        assert app.is_running
        assert tabs.active == ""
        query_action = app.query_one("#new-query", Button)
        source_action = app.query_one("#open-sources", Button)
        await wait_for(lambda: query_action.has_focus, pilot)
        assert str(query_action.label) == "Query"
        assert str(source_action.label) == "Open Sources"
        assert app.screen.region.contains_region(query_action.region)
        assert app.screen.region.contains_region(source_action.region)
        await pilot.press("ctrl+enter", "ctrl+w", "tab")
        assert tabs.tab_count == 0
        assert source_action.has_focus
        await pilot.press("shift+tab", "enter")
        await wait_for(lambda: tabs.active == "query-1", pilot)
        query = tabs.active_pane
        assert isinstance(query, QueryWorkspace)
        await wait_for(lambda: query.editor.has_focus, pilot)
        assert query.editor.text == ""
        assert query.current_result is None
        query.editor.load_text('SELECT count(*) AS total FROM "smoke"')
        await pilot.press("ctrl+enter")
        await wait_for(lambda: bool(query.query(ArrowTable)), pilot)
        assert query.query_one(ArrowTable).get_cell_at(Coordinate(0, 0)).as_py() == 5
        assert tabs.tab_count == 1


async def test_last_query_requires_confirmation_and_palette_starts_fresh(
    small_parquet: Path,
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test() as pilot:
        table = await run_query(app, pilot, "SELECT sum(id) FROM smoke")
        tabs = app.query_one(TabbedContent)
        query = tabs.active_pane
        assert isinstance(query, QueryWorkspace)
        await select_tab(tabs, "source-1", pilot)
        await pilot.press("ctrl+w")
        await wait_for(lambda: tabs.tab_count == 1, pilot)
        assert tabs.active_pane is query
        await pilot.press("ctrl+w")
        await wait_for(lambda: app.screen.is_modal, pilot)
        assert str(app.screen.query_one("#cancel", Button).label) == "Cancel"
        assert app.screen.query_one("#cancel", Button).has_focus
        await pilot.press("escape")
        await wait_for(lambda: not app.screen.is_modal, pilot)
        assert query.editor.text == "SELECT sum(id) FROM smoke"
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == 15
        await pilot.press("ctrl+w")
        await confirm_query_close(app, pilot)
        await wait_for(lambda: tabs.tab_count == 0, pilot)
        assert table.row_count == 0
        await pilot.press("ctrl+enter", "ctrl+w")
        fresh = await open_sql_query(pilot)
        assert tabs.active == "query-2"
        assert fresh.editor.text == ""
        assert fresh.current_result is None
        await pilot.press("ctrl+w")
        await wait_for(lambda: tabs.tab_count == 0, pilot)
        assert not app.screen.is_modal


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
@pytest.mark.parametrize("sidebar_visible", [False, True])
async def test_empty_state_reopens_sources_once_in_default_browsing_state(
    small_parquet: Path, size: tuple[int, int], sidebar_visible: bool
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test(size=size) as pilot:
        tabs = app.query_one(TabbedContent)
        await wait_for(lambda: bool(tabs.query(ArrowTable)), pilot)
        table = tabs.get_pane("source-1").query_one(ArrowTable)
        table.focus()
        await pilot.press("h", "down", "down")
        assert not table.show_header
        assert table.cursor_coordinate == Coordinate(2, 0)
        sources = app.query_one(SourcesTree)
        if sidebar_visible:
            app.action_toggle_sources()
            sources.focus()
        await pilot.press("ctrl+w")
        await wait_for(lambda: tabs.tab_count == 0, pilot)
        query_action = app.query_one("#new-query", Button)
        source_action = app.query_one("#open-sources", Button)
        if sidebar_visible:
            assert sources.has_focus
            await pilot.press("tab")
            assert query_action.has_focus
        await pilot.press("tab")
        assert source_action.has_focus
        assert app.screen.region.contains_region(source_action.region)
        await pilot.press("enter")
        await wait_for(lambda: sources.display and sources.has_focus, pilot)
        await pilot.press("home", "right")
        assert str(sources.root.children[0].children[1].label) == 'Table: "smoke"'
        assert "id: int64" in [
            str(child.label) for child in sources.root.children[0].children[1].children
        ]
        assert not sources.region.overlaps(query_action.region)
        assert app.screen.region.contains_region(query_action.region)
        await pilot.press("enter", "enter")
        await wait_for(lambda: bool(tabs.query("#source-1 ArrowTable")), pilot)
        reopened = tabs.get_pane("source-1").query_one(ArrowTable)
        cache = reopened.data
        assert isinstance(cache, PageCache)
        await wait_for(lambda: cache.peek(0, 0) is not None, pilot)
        assert reopened.get_cell_at(Coordinate(0, 0)).as_py() == 1
        assert reopened.show_header
        assert reopened.cursor_coordinate == Coordinate(0, 0)
        assert tabs.tab_count == 1
        assert len(sources.root.children) == 1
        assert str(tabs.get_tab("source-1").label) == "smoke.parquet"
        assert sources.has_focus
        await pilot.press("ctrl+w")
        await wait_for(lambda: tabs.tab_count == 0, pilot)
        assert sources.has_focus
        await pilot.press("enter")
        await wait_for(lambda: bool(tabs.query("#source-1 ArrowTable")), pilot)
        assert tabs.tab_count == 1


@pytest.mark.parametrize("fail", [False, True])
async def test_source_load_finishes_after_all_views_close_without_reopening_them(
    small_parquet: Path, fail: bool
) -> None:
    pending = small_parquet.with_name("pending.parquet")
    pending.write_bytes(b"invalid parquet" if fail else small_parquet.read_bytes())
    started, release = Event(), Event()
    execute = duckdb.DuckDBPyConnection.execute

    def delayed_metadata(
        connection: duckdb.DuckDBPyConnection, query: str, parameters: Any = None
    ) -> duckdb.DuckDBPyConnection:
        if "parquet_schema" in query and parameters[0] == str(pending):
            started.set()
            assert release.wait(timeout=15)
        return execute(connection, query, parameters)

    app = ParqxApp([pending, small_parquet])
    with patch.object(duckdb.DuckDBPyConnection, "execute", delayed_metadata):
        async with app.run_test() as pilot:
            try:
                tabs = app.query_one(TabbedContent)
                await wait_for(started.is_set, pilot)
                await wait_for(lambda: bool(tabs.query("#source-2 ArrowTable")), pilot)
                closed = tabs.get_pane("source-1")
                app.action_toggle_sources()
                sources = app.query_one(SourcesTree)
                sources.focus()
                await pilot.press("home", "ctrl+w")
                await wait_for(lambda: tabs.tab_count == 1, pilot)
                await pilot.press("ctrl+w")
                await wait_for(lambda: tabs.tab_count == 0, pilot)
                assert "loading" in str(sources.root.children[0].label)
                assert sources.has_focus
                release.set()
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                state = "failed" if fail else "ready"
                assert (
                    str(sources.root.children[0].label) == f"pending.parquet · {state}"
                )
                assert app.is_running
                assert tabs.tab_count == 0
                assert not closed.is_attached
                assert not closed.query(ArrowTable)
                assert sources.has_focus
                await pilot.press("enter", "enter")
                await wait_for(lambda: tabs.active == "source-1", pilot)
                if fail:
                    pane = tabs.get_pane("source-1")
                    error = pane.query_one(".source-error", Static)
                    assert app.load_errors[0].message in str(error.content)
                    assert not pane.loading
                    assert not pane.query(ArrowTable)
                else:
                    await wait_for(lambda: bool(tabs.query(ArrowTable)), pilot)
                    table = tabs.get_pane("source-1").query_one(ArrowTable)
                    cache = table.data
                    assert isinstance(cache, PageCache)
                    await wait_for(lambda: cache.peek(0, 0) is not None, pilot)
                    assert table.get_cell_at(Coordinate(0, 0)).as_py() == 1
                    assert "id: int64" in [
                        str(child.label)
                        for child in sources.root.children[0].children[1].children
                    ]
                assert tabs.tab_count == 1
                assert len(sources.root.children) == 2
            finally:
                release.set()


async def test_sidebar_shortcuts_use_active_workspace_and_never_selected_source(
    small_parquet: Path,
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test() as pilot:
        table = await run_query(app, pilot, "SELECT sum(id) FROM smoke")
        tabs = app.query_one(TabbedContent)
        query = tabs.active_pane
        assert isinstance(query, QueryWorkspace)
        query.editor.load_text("SELECT count(*) AS total FROM smoke")
        app.action_toggle_sources()
        sources = app.query_one(SourcesTree)
        sources.focus()
        await pilot.press("home", "enter")
        await wait_for(lambda: tabs.active == "source-1", pilot)
        await pilot.press("ctrl+enter")
        assert not query.running
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == 15
        await select_tab(tabs, "query-1", pilot)
        sources.focus()
        await pilot.press("h", "i", "z", "c", "ctrl+enter")
        await wait_for(lambda: table.columns[0].name == "total", pilot)
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == 5
        assert query.editor.text == "SELECT count(*) AS total FROM smoke"
        assert table.show_header
        assert table.show_row_index
        assert not table.zebra_stripes
        assert table.cursor_type == "cell"
        assert sources.has_focus
        await pilot.press("ctrl+w")
        await confirm_query_close(app, pilot)
        await wait_for(lambda: tabs.tab_count == 1, pilot)
        assert tabs.active == "source-1"
        assert len(sources.root.children) == 1


async def test_last_running_workspace_stays_open_until_execution_finishes(
    small_parquet: Path,
) -> None:
    started, release = Event(), Event()
    sql = duckdb.DuckDBPyConnection.sql

    def delayed_query(
        connection: duckdb.DuckDBPyConnection, query: str
    ) -> duckdb.DuckDBPyRelation:
        started.set()
        assert release.wait(timeout=15)
        return sql(connection, query)

    app = ParqxApp([small_parquet])
    with patch.object(duckdb.DuckDBPyConnection, "sql", delayed_query):
        async with app.run_test() as pilot:
            try:
                tabs = app.query_one(TabbedContent)
                await wait_for(lambda: bool(tabs.query(ArrowTable)), pilot)
                await pilot.press("ctrl+w")
                await wait_for(lambda: tabs.tab_count == 0, pilot)
                query = await open_query(app, pilot)
                query.editor.load_text("SELECT 42 AS answer")
                await pilot.press("ctrl+enter")
                await wait_for(started.is_set, pilot)
                await pilot.press("ctrl+w")
                assert not app.screen.is_modal
                assert query.editor.read_only
                assert tabs.tab_count == 1
                assert tabs.active_pane is query
                release.set()
                await wait_for(lambda: not query.running, pilot)
                assert (
                    query.query_one(ArrowTable).get_cell_at(Coordinate(0, 0)).as_py()
                    == 42
                )
                await pilot.press("ctrl+w")
                await confirm_query_close(app, pilot)
                await wait_for(lambda: tabs.tab_count == 0, pilot)
            finally:
                release.set()
