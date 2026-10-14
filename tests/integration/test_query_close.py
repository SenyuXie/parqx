"""Discard confirmation through the active tab's public input and focus paths."""

from pathlib import Path
from threading import Event
from unittest.mock import patch

import duckdb
import pytest
from textual.coordinate import Coordinate
from textual.widgets import Button, Static, TabbedContent, TextArea

from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from parqx.tui.widgets.sources_tree import SourcesTree
from tests.helpers import (
    confirm_query_close,
    open_query,
    run_query,
    select_tab,
    wait_for,
)


@pytest.mark.parametrize("focus", ["editor", "result", "tabs", "sources"])
async def test_close_confirmation_cancellation_preserves_workspace(
    small_parquet: Path, focus: str
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test(size=(80, 24)) as pilot:
        table = await run_query(app, pilot, "SELECT name FROM smoke ORDER BY id")
        tabs = app.query_one(TabbedContent)
        query = tabs.active_pane
        assert query is not None
        editor = query.query_one(TextArea)
        editor.focus()
        await pilot.press("ctrl+end", "space", "-", "-", "space", "x")
        editor.history.checkpoint()
        await pilot.press("shift+left")
        assert editor.selected_text == "x"
        draft, selection = editor.text, editor.selection
        table.focus()
        await pilot.press("down", "down")
        assert table.cursor_coordinate == Coordinate(2, 0)
        if focus == "editor":
            editor.focus()
        elif focus == "tabs":
            await select_tab(tabs, "query-1", pilot)
        elif focus == "sources":
            app.action_toggle_sources()
            sources = app.query_one(SourcesTree)
            sources.focus()
            await pilot.press("right", "down", "left")
            assert sources.cursor_node is not None
            assert sources.cursor_node.data == "source-1"
        await pilot.pause()
        focused = app.focused
        assert focused is not None
        layout = (editor.region, table.region)
        await pilot.press("ctrl+w")
        assert app.screen.is_modal
        assert editor.text == draft
        text = " ".join(str(widget.content) for widget in app.screen.query(Static))
        assert "Close Query 1?" in text
        assert "SQL draft" in text
        assert "query result" in text
        assert isinstance(app.focused, Button)
        assert str(app.focused.label) == "Cancel"
        await pilot.press("enter")
        await wait_for(lambda: not app.screen.is_modal, pilot)
        assert tabs.active == "query-1"
        assert app.focused is focused
        assert editor.text == draft
        assert editor.selection == selection
        assert (editor.region, table.region) == layout
        assert table.cursor_coordinate == Coordinate(2, 0)
        assert table.get_cell_at(table.cursor_coordinate).as_py() == "carol"
        await pilot.press("ctrl+w", "escape")
        await wait_for(lambda: not app.screen.is_modal, pilot)
        assert tabs.active == "query-1"
        assert tabs.tab_count == 2
        assert editor.text == draft
        assert editor.selection == selection
        editor.focus()
        await pilot.press("ctrl+z")
        assert editor.text == "SELECT name FROM smoke ORDER BY id"
        await pilot.press("ctrl+y")
        assert editor.text == draft
        focused.focus()
        await pilot.press("ctrl+w")
        await confirm_query_close(app, pilot)
        await wait_for(lambda: tabs.tab_count == 1, pilot)
        assert tabs.active == "source-1"


async def test_result_without_sql_still_requires_explicit_discard(
    small_parquet: Path,
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test() as pilot:
        table = await run_query(app, pilot, "SELECT 42 AS answer")
        tabs = app.query_one(TabbedContent)
        query = tabs.active_pane
        assert query is not None
        query.query_one(TextArea).load_text(" \n ")
        await pilot.press("ctrl+w")
        assert app.screen.is_modal
        await pilot.press("escape")
        await wait_for(lambda: not app.screen.is_modal, pilot)
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == 42
        await pilot.press("ctrl+w")
        await confirm_query_close(app, pilot)
        await wait_for(lambda: tabs.tab_count == 1, pilot)
        assert tabs.active == "source-1"
        assert table.row_count == 0


async def test_empty_workspaces_and_file_views_close_without_confirmation(
    small_parquet: Path,
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test() as pilot:
        tabs = app.query_one(TabbedContent)
        await wait_for(lambda: not tabs.get_pane("source-1").loading, pilot)
        for draft in ("", " \n\t "):
            query = await open_query(app, pilot)
            query.editor.load_text(draft)
            await pilot.press("ctrl+w")
            await wait_for(lambda: tabs.tab_count == 1, pilot)
            assert not app.screen.is_modal
            assert tabs.active == "source-1"
        query = await open_query(app, pilot)
        await select_tab(tabs, "source-1", pilot)
        await pilot.press("ctrl+w")
        await wait_for(lambda: tabs.tab_count == 1, pilot)
        assert not app.screen.is_modal
        assert tabs.active_pane is query
        query.editor.load_text("SELECT count(*) AS total FROM smoke")
        query.editor.focus()
        await pilot.press("ctrl+enter")
        await wait_for(lambda: bool(query.query(ArrowTable)), pilot)
        assert query.query_one(ArrowTable).get_cell_at(Coordinate(0, 0)).as_py() == 5


@pytest.mark.parametrize("fail", [False, True])
async def test_background_completion_preserves_close_target_and_modal_focus(
    small_parquet: Path, fail: bool
) -> None:
    started, release = Event(), Event()
    original_sql = duckdb.DuckDBPyConnection.sql

    def delayed_sql(
        connection: duckdb.DuckDBPyConnection, sql: str
    ) -> duckdb.DuckDBPyRelation:
        started.set()
        assert release.wait(timeout=15)
        if fail:
            raise RuntimeError("background query failed")
        return original_sql(connection, sql)

    app = ParqxApp([small_parquet])
    with patch.object(duckdb.DuckDBPyConnection, "sql", delayed_sql):
        async with app.run_test() as pilot:
            try:
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                tabs = app.query_one(TabbedContent)
                running = await open_query(app, pilot)
                running.editor.load_text("SELECT 42 AS answer")
                await pilot.press("ctrl+enter")
                await wait_for(started.is_set, pilot)
                await pilot.press("ctrl+w")
                assert not app.screen.is_modal
                assert tabs.active_pane is running
                assert running.editor.read_only
                target = await open_query(app, pilot)
                target.editor.load_text("SELECT 17")
                await pilot.press("ctrl+w")
                modal = app.screen
                assert modal.is_modal
                assert isinstance(app.focused, Button)
                assert str(app.focused.label) == "Cancel"
                cancel = app.focused
                await pilot.press("ctrl+w", "ctrl+w", "ctrl+enter")
                assert app.screen is modal
                assert app.focused is cancel
                assert tabs.tab_count == 3
                assert not target.running
                assert target.editor.text == "SELECT 17"
                release.set()
                await wait_for(lambda: not running.running, pilot)
                assert app.screen is modal
                assert app.focused is cancel
                assert tabs.active_pane is target
                assert not target.query(ArrowTable)
                if fail:
                    assert not running.query(ArrowTable)
                else:
                    assert (
                        running.query_one(ArrowTable)
                        .get_cell_at(Coordinate(0, 0))
                        .as_py()
                        == 42
                    )
                await pilot.press("tab")
                assert isinstance(app.focused, Button)
                assert str(app.focused.label) == "Close"
                await pilot.press("enter")
                await wait_for(lambda: tabs.tab_count == 2, pilot)
                assert not tabs.query("#query-2")
                assert tabs.get_pane("query-1") is running
                assert tabs.get_pane("source-1").is_attached
                assert not app.screen.is_modal
            finally:
                release.set()
