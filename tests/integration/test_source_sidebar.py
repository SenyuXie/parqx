"""Inspect session schemas through the mounted application's shared sidebar."""

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
from textual.command import CommandList, CommandPalette
from textual.coordinate import Coordinate
from textual.pilot import Pilot
from textual.widgets import Input, TabbedContent

from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from parqx.tui.widgets.sources_tree import SourcesTree
from tests.helpers import run_query, select_tab, wait_for


async def toggle_sources(app: ParqxApp, pilot: Pilot[None]) -> None:
    await pilot.press("ctrl+p")
    palette = app.screen
    assert isinstance(palette, CommandPalette)
    palette.query_one(Input).value = "Toggle Sources"
    commands = palette.query_one(CommandList)
    await wait_for(lambda: commands.option_count == 1, pilot)
    await pilot.press("down", "enter")
    await wait_for(lambda: app.screen is not palette, pilot)


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_sources_visibility_is_shared_and_returns_main_view_space(
    small_parquet: Path, size: tuple[int, int]
) -> None:
    other = small_parquet.with_name("orders_with_a_long_filename_and_schema.parquet")
    pq.write_table(
        pa.table({f"long_column_name_{index}": [index] for index in range(50)}), other
    )
    app = ParqxApp([small_parquet, other])
    async with app.run_test(size=size) as pilot:
        await wait_for(lambda: len(app.query(ArrowTable)) == 2, pilot)
        tabs = app.query_one(TabbedContent)
        sources = app.query_one(SourcesTree)
        assert not sources.display
        original_width = tabs.region.width
        await toggle_sources(app, pilot)
        assert sources.display
        assert sources.region.width > 0
        assert tabs.region.width < original_width
        assert sources.region.right <= tabs.region.x
        await select_tab(tabs, "source-2", pilot)
        assert sources.display
        sources.focus()
        await pilot.press("end", "right")
        await pilot.pause()
        assert sources.virtual_size.width > sources.content_size.width
        assert sources.show_horizontal_scrollbar
        assert sources.show_vertical_scrollbar
        await toggle_sources(app, pilot)
        assert not sources.display
        assert tabs.region.width == original_width


async def test_sidebar_keeps_duplicate_source_identity_and_schema_after_view_closes(
    tmp_path: Path,
) -> None:
    first = tmp_path / "one" / 'sales [report]-"items".parquet'
    second = tmp_path / "two" / first.name
    for path in (first, second):
        path.parent.mkdir()
    pq.write_table(pa.table({"id": [1], "name": ["Alice"]}), first)
    pq.write_table(pa.table({"amount": [2.5]}), second)
    app = ParqxApp([second, first, second])
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: len(app.query(ArrowTable)) == 2, pilot)
        await toggle_sources(app, pilot)
        sources = app.query_one(SourcesTree)
        nodes = sources.root.children
        assert len(nodes) == 2
        assert [node.data for node in nodes] == ["source-1", "source-2"]
        assert [str(node.label) for node in nodes] == [
            f"{second.name} · ready",
            f"{first.name} · ready",
        ]
        assert [str(child.label) for child in nodes[0].children] == [
            f"File: {second.resolve()}",
            'SQL: "sales [report]-""items"""',
            "amount: double",
        ]
        assert [str(child.label) for child in nodes[1].children] == [
            f"File: {first.resolve()}",
            'SQL: "sales [report]-""items""_2"',
            "id: int64",
            "name: string",
        ]
        tabs = app.query_one(TabbedContent)
        table = tabs.get_pane("source-1").query_one(ArrowTable)
        cache = weakref.ref(table.data)
        await pilot.press("ctrl+w")
        await wait_for(lambda: not tabs.query("#source-1"), pilot)

        def released() -> bool:
            gc.collect()
            return cache() is None

        await wait_for(released, pilot)
        assert "amount: double" in [str(child.label) for child in nodes[0].children]
        result = await run_query(
            app, pilot, 'SELECT amount FROM "sales [report]-""items"""'
        )
        assert result.get_cell_at(Coordinate(0, 0)).as_py() == 2.5
        assert not tabs.query("#source-1")


async def test_source_tree_uses_left_right_and_native_focus_without_table_actions(
    small_parquet: Path,
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        await toggle_sources(app, pilot)
        sources = app.query_one(SourcesTree)
        table = app.query_one(ArrowTable)
        table.focus()
        await pilot.press("shift+tab", "shift+tab")
        assert sources.has_focus
        await pilot.press("home", "right")
        node = sources.root.children[0]
        assert node.is_expanded
        await pilot.press("right")
        assert sources.cursor_node is node.children[0]
        await pilot.press("left")
        assert sources.cursor_node is node
        await pilot.press("left")
        assert not node.is_expanded
        await pilot.press("right", "down", "down", "down", "h", "i", "z", "c")
        assert sources.cursor_node is not None
        assert str(sources.cursor_node.label) == "id: int64"
        assert table.show_header
        assert table.show_row_index
        assert not table.zebra_stripes
        assert table.cursor_type == "cell"
        await toggle_sources(app, pilot)
        assert not sources.display
        assert not sources.has_focus
        assert sources not in app.screen.focus_chain
        assert app.focused in app.screen.focus_chain


async def test_closed_loading_sources_finish_as_ready_or_failed_without_new_views(
    small_parquet: Path,
) -> None:
    pending = small_parquet.with_name("pending.parquet")
    failed = small_parquet.with_name("failed.parquet")
    pending.write_bytes(small_parquet.read_bytes())
    failed.write_text("Not a parquet file", encoding="utf-8")
    started = {str(path): Event() for path in (pending, failed)}
    release = Event()
    execute = duckdb.DuckDBPyConnection.execute

    def delayed_metadata(
        connection: duckdb.DuckDBPyConnection, query: str, parameters: Any = None
    ) -> duckdb.DuckDBPyConnection:
        if "parquet_schema" in query and parameters[0] in started:
            started[parameters[0]].set()
            assert release.wait(timeout=15)
        return execute(connection, query, parameters)

    app = ParqxApp([pending, failed, small_parquet])
    with patch.object(duckdb.DuckDBPyConnection, "execute", delayed_metadata):
        async with app.run_test(size=(120, 40)) as pilot:
            try:
                await wait_for(
                    lambda: all(event.is_set() for event in started.values()), pilot
                )
                await toggle_sources(app, pilot)
                sources = app.query_one(SourcesTree)
                pending_node, failed_node, _ = sources.root.children
                assert str(pending_node.label) == "pending.parquet · loading"
                assert str(failed_node.label) == "failed.parquet · loading"
                assert [str(child.label) for child in pending_node.children][
                    -1
                ] == "Loading schema…"
                sources.focus()
                await pilot.press("home", "right", "down", "enter")
                assert app.catalog.snapshot() == (app.catalog.get("source-3").spec,)
                tabs = app.query_one(TabbedContent)
                await pilot.press("ctrl+w")
                await wait_for(lambda: not tabs.query("#source-1"), pilot)
                await select_tab(tabs, "source-2", pilot)
                await pilot.press("ctrl+w")
                await wait_for(lambda: not tabs.query("#source-2"), pilot)
                sources.focus()
                release.set()
                await wait_for(
                    lambda: all(
                        entry.state != "loading" for entry in app.catalog.entries
                    ),
                    pilot,
                )
                assert sources.has_focus
                assert str(pending_node.label) == "pending.parquet · ready"
                assert [str(child.label) for child in pending_node.children][-3:] == [
                    "id: int64",
                    "name: string",
                    "score: double",
                ]
                assert str(failed_node.label) == "failed.parquet · failed"
                issue = app.load_errors[0]
                assert [str(child.label) for child in failed_node.children][
                    -1
                ] == f"Error: {issue.message}"
                assert app.catalog.get("source-2").schema is None
                assert tabs.tab_count == 1
                assert tabs.active == "source-3"
                result = await run_query(app, pilot, 'SELECT count(*) FROM "pending"')
                assert result.get_cell_at(Coordinate(0, 0)).as_py() == 5
                assert not tabs.query("#source-1, #source-2")
            finally:
                release.set()


async def test_metadata_arrival_preserves_sidebar_inspection_focus(
    small_parquet: Path,
) -> None:
    started, release = Event(), Event()
    execute = duckdb.DuckDBPyConnection.execute

    def delayed_metadata(
        connection: duckdb.DuckDBPyConnection, query: str, parameters: Any = None
    ) -> duckdb.DuckDBPyConnection:
        if "parquet_schema" in query:
            started.set()
            assert release.wait(timeout=15)
        return execute(connection, query, parameters)

    app = ParqxApp([small_parquet])
    with patch.object(duckdb.DuckDBPyConnection, "execute", delayed_metadata):
        async with app.run_test() as pilot:
            try:
                await wait_for(started.is_set, pilot)
                await toggle_sources(app, pilot)
                sources = app.query_one(SourcesTree)
                sources.focus()
                await pilot.press("home", "right", "down")
                assert sources.cursor_node is not None
                assert (
                    str(sources.cursor_node.label) == f"File: {small_parquet.resolve()}"
                )
                release.set()
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                assert sources.has_focus
                assert sources.cursor_node is not None
                assert (
                    str(sources.cursor_node.label) == f"File: {small_parquet.resolve()}"
                )
            finally:
                release.set()
