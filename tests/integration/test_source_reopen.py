"""Recover original file views through the mounted application's sources sidebar."""

import gc
import weakref
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from textual.coordinate import Coordinate
from textual.widgets import Static, TabbedContent

from parqx.data.view import PageCache
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from parqx.tui.widgets.sources_tree import SourcesTree
from tests.helpers import open_query, run_query, select_tab, wait_for


async def test_sidebar_activates_and_reopens_one_default_view_per_source(
    tmp_path: Path,
) -> None:
    first = tmp_path / "one" / "sales.parquet"
    second = tmp_path / "two" / first.name
    for path in (first, second):
        path.parent.mkdir()
    pq.write_table(pa.table({"amount": [10, 20, 30]}), first)
    pq.write_table(pa.table({"amount": [40]}), second)
    app = ParqxApp([first, second, first])
    async with app.run_test(size=(120, 40)) as pilot:
        tabs = app.query_one(TabbedContent)
        await wait_for(lambda: len(tabs.query(ArrowTable)) == 2, pilot)
        table = tabs.get_pane("source-1").query_one(ArrowTable)
        table.focus()
        await pilot.press("h", "i", "z", "c", "down")
        await select_tab(tabs, "source-2", pilot)
        app.action_toggle_sources()
        sources = app.query_one(SourcesTree)
        sources.focus()
        await pilot.press("home", "enter")
        await wait_for(lambda: tabs.active == "source-1", pilot)
        assert tabs.tab_count == 2
        assert tabs.get_pane("source-1").query_one(ArrowTable) is table
        assert not table.show_header
        assert table.cursor_coordinate == Coordinate(1, 0)
        cache = weakref.ref(table.data)
        await pilot.press("ctrl+w")
        await wait_for(lambda: not tabs.query("#source-1"), pilot)

        def released() -> bool:
            gc.collect()
            return cache() is None

        await wait_for(released, pilot)
        assert "amount: int64" in [
            str(child.label) for child in sources.root.children[0].children
        ]
        sources.focus()
        await pilot.press("home", "enter", "enter")
        await wait_for(
            lambda: (
                tabs.active == "source-1" and bool(tabs.query("#source-1 ArrowTable"))
            ),
            pilot,
        )
        reopened = tabs.get_pane("source-1").query_one(ArrowTable)
        reopened_cache = reopened.data
        assert isinstance(reopened_cache, PageCache)
        await wait_for(lambda: reopened_cache.peek(0, 0) is not None, pilot)
        assert reopened.get_cell_at(Coordinate(0, 0)).as_py() == 10
        assert reopened.show_header
        assert reopened.show_row_index
        assert not reopened.zebra_stripes
        assert reopened.cursor_type == "cell"
        assert reopened.cursor_coordinate == Coordinate(0, 0)
        assert tabs.tab_count == 2
        assert len(app.catalog.entries) == 2
        assert [entry.spec.table_name for entry in app.catalog.entries] == [
            "sales",
            "sales_2",
        ]
        assert '"sales"' in str(tabs.get_tab("source-1").label)
        assert str(first.resolve()) in str(tabs.get_tab("source-1").tooltip)
        sources.focus()
        await pilot.press("home", "right", "down", "down", "down", "enter")
        assert tabs.active == "source-1"
        assert tabs.tab_count == 2
        result = await run_query(app, pilot, 'SELECT sum(amount) FROM "sales"')
        assert result.get_cell_at(Coordinate(0, 0)).as_py() == 60


@pytest.mark.parametrize("initial_loading", [True, False])
async def test_reopening_pending_source_shares_load_and_never_resurrects_closed_view(
    small_parquet: Path, initial_loading: bool
) -> None:
    pending = small_parquet.with_name("pending.parquet")
    pending.write_bytes(small_parquet.read_bytes())
    started, release, block = Event(), Event(), Event()
    if initial_loading:
        block.set()
    metadata_reads = 0
    execute = duckdb.DuckDBPyConnection.execute

    def delayed_metadata(
        connection: duckdb.DuckDBPyConnection, query: str, parameters: Any = None
    ) -> duckdb.DuckDBPyConnection:
        nonlocal metadata_reads
        if (
            "parquet_schema" in query
            and parameters[0] == str(pending)
            and block.is_set()
        ):
            metadata_reads += 1
            started.set()
            assert release.wait(timeout=15)
        return execute(connection, query, parameters)

    app = ParqxApp([pending, small_parquet])
    with patch.object(duckdb.DuckDBPyConnection, "execute", delayed_metadata):
        async with app.run_test(size=(120, 40)) as pilot:
            try:
                tabs = app.query_one(TabbedContent)
                app.action_toggle_sources()
                sources = app.query_one(SourcesTree)
                if not initial_loading:
                    await wait_for(lambda: len(app.query(ArrowTable)) == 2, pilot)
                    block.set()
                    await pilot.press("ctrl+w")
                    await wait_for(lambda: not tabs.query("#source-1"), pilot)
                    sources.focus()
                    await pilot.press("home", "enter")
                await wait_for(started.is_set, pilot)
                await wait_for(
                    lambda: app.catalog.get("source-2").state == "ready", pilot
                )
                await pilot.press("ctrl+w")
                await wait_for(lambda: not tabs.query("#source-1"), pilot)
                sources.focus()
                await pilot.press("home", "enter", "enter")
                await wait_for(lambda: tabs.active == "source-1", pilot)
                assert tabs.get_pane("source-1").loading
                assert tabs.tab_count == 2
                await pilot.press("ctrl+w")
                await wait_for(lambda: not tabs.query("#source-1"), pilot)
                release.set()
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                assert metadata_reads == 1
                assert tabs.tab_count == 1
                assert tabs.active == "source-2"
                assert str(sources.root.children[0].label) == "pending.parquet · ready"
                assert "id: int64" in [
                    str(child.label) for child in sources.root.children[0].children
                ]
                sources.focus()
                await pilot.press("home", "enter")
                await wait_for(lambda: bool(tabs.query("#source-1 ArrowTable")), pilot)
                table = tabs.get_pane("source-1").query_one(ArrowTable)
                page_cache = table.data
                assert isinstance(page_cache, PageCache)
                await wait_for(lambda: page_cache.peek(0, 0) is not None, pilot)
                assert table.get_cell_at(Coordinate(0, 0)).as_py() == 1
                assert tabs.tab_count == 2
                assert len(app.catalog.entries) == 2
            finally:
                release.set()


async def test_failed_source_reopens_stored_failure_without_retrying(
    small_parquet: Path,
) -> None:
    failed = small_parquet.with_name("failed.parquet")
    failed.write_text("Not a parquet file", encoding="utf-8")
    app = ParqxApp([failed, small_parquet])
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: app.catalog.get("source-1").state == "failed", pilot)
        tabs = app.query_one(TabbedContent)
        issue = app.load_errors[0]
        await pilot.press("ctrl+w")
        await wait_for(lambda: not tabs.query("#source-1"), pilot)
        # Reopening a failed catalog entry must not silently become a retry policy.
        failed.write_bytes(small_parquet.read_bytes())
        app.action_toggle_sources()
        sources = app.query_one(SourcesTree)
        sources.focus()
        await pilot.press("home", "enter", "enter")
        await wait_for(lambda: tabs.active == "source-1", pilot)
        pane = tabs.get_pane("source-1")
        assert not pane.loading
        assert not pane.query(ArrowTable)
        error = pane.query_one(".source-error", Static)
        assert error.display
        assert str(failed.resolve()) in str(error.content)
        assert issue.message in str(error.content)
        assert str(sources.root.children[0].label) == "failed.parquet · failed"
        assert app.load_errors == (issue,)
        assert app.catalog.get("source-1").schema is None
        assert tabs.tab_count == 2
        result = await run_query(app, pilot, 'SELECT count(*) FROM "smoke"')
        assert result.get_cell_at(Coordinate(0, 0)).as_py() == 5


async def test_source_reopening_preserves_running_query_owner_and_other_drafts(
    small_parquet: Path,
) -> None:
    other = small_parquet.with_name("other.parquet")
    other.write_bytes(small_parquet.read_bytes())
    started, release = Event(), Event()
    sql = duckdb.DuckDBPyConnection.sql

    def delayed_query(
        connection: duckdb.DuckDBPyConnection, query: str
    ) -> duckdb.DuckDBPyRelation:
        started.set()
        assert release.wait(timeout=15)
        return sql(connection, query)

    app = ParqxApp([small_parquet, other])
    async with app.run_test(size=(120, 40)) as pilot:
        try:
            tabs = app.query_one(TabbedContent)
            await wait_for(lambda: len(tabs.query(ArrowTable)) == 2, pilot)
            await pilot.press("ctrl+w")
            await wait_for(lambda: not tabs.query("#source-1"), pilot)
            executing = await open_query(app, pilot)
            executing.editor.load_text('SELECT sum(id) FROM "smoke"')
            with patch.object(duckdb.DuckDBPyConnection, "sql", delayed_query):
                await pilot.press("ctrl+enter")
                await wait_for(started.is_set, pilot)
                draft = await open_query(app, pilot)
                draft.editor.load_text("SELECT name FROM other")
                app.action_toggle_sources()
                sources = app.query_one(SourcesTree)
                sources.focus()
                await pilot.press("home", "right", "down", "down", "down", "enter")
                assert tabs.active_pane is draft
                assert draft.editor.text == "SELECT name FROM other"
                await pilot.press("home", "enter")
                await wait_for(lambda: tabs.active == "source-1", pilot)
                await wait_for(lambda: bool(tabs.query("#source-1 ArrowTable")), pilot)
                assert executing.running
                assert sources.has_focus
                release.set()
                await wait_for(lambda: not executing.running, pilot)
            assert tabs.active == "source-1"
            assert sources.has_focus
            assert (
                executing.query_one(ArrowTable).get_cell_at(Coordinate(0, 0)).as_py()
                == 15
            )
            assert draft.editor.text == "SELECT name FROM other"
            assert not draft.query(ArrowTable)
            assert tabs.tab_count == 4
        finally:
            release.set()


async def test_pending_failure_updates_the_explicitly_reopened_view(
    small_parquet: Path,
) -> None:
    failed = small_parquet.with_name("failed.parquet")
    failed.write_text("Not a parquet file", encoding="utf-8")
    started, release = Event(), Event()
    execute = duckdb.DuckDBPyConnection.execute

    def delayed_metadata(
        connection: duckdb.DuckDBPyConnection, query: str, parameters: Any = None
    ) -> duckdb.DuckDBPyConnection:
        if "parquet_schema" in query and parameters[0] == str(failed):
            started.set()
            assert release.wait(timeout=15)
        return execute(connection, query, parameters)

    app = ParqxApp([failed, small_parquet])
    with patch.object(duckdb.DuckDBPyConnection, "execute", delayed_metadata):
        async with app.run_test(size=(120, 40)) as pilot:
            try:
                tabs = app.query_one(TabbedContent)
                await wait_for(started.is_set, pilot)
                await pilot.press("ctrl+w")
                await wait_for(lambda: not tabs.query("#source-1"), pilot)
                app.action_toggle_sources()
                sources = app.query_one(SourcesTree)
                sources.focus()
                await pilot.press("home", "enter", "enter")
                await wait_for(lambda: tabs.active == "source-1", pilot)
                assert tabs.get_pane("source-1").loading
                release.set()
                await wait_for(
                    lambda: app.catalog.get("source-1").state == "failed", pilot
                )
                pane = tabs.get_pane("source-1")
                assert not pane.loading
                error = pane.query_one(".source-error", Static)
                assert error.display
                assert app.load_errors[0].message in str(error.content)
                assert not pane.query(ArrowTable)
                assert tabs.tab_count == 2
                assert str(sources.root.children[0].label) == "failed.parquet · failed"
            finally:
                release.set()
