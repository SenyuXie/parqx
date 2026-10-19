"""Persistent SQL workspaces exercised through the mounted application."""

from pathlib import Path

import pytest
from textual.widgets import TabbedContent, TextArea

from parqx.tui.app import Parqx
from tests.helpers import confirm_query_close, open_sql_query, select_tab, wait_for


async def test_query_palette_opens_independent_blank_workspaces(
    small_parquet: Path,
) -> None:
    app = Parqx([small_parquet])
    async with app.run_test(size=(80, 24)) as pilot:
        tabs = app.query_one(TabbedContent)
        await wait_for(lambda: not tabs.get_pane("source-1").loading, pilot)
        for number in (1, 2):
            query = await open_sql_query(pilot)
            assert tabs.active == f"query-{number}"
            editor = query.editor
            assert editor.text == ""
            assert not app.screen.is_modal
            assert str(tabs.get_tab(f"query-{number}").label) == f"Query {number}"
            await pilot.press("S", "E", "L", "E", "C", "T", "space", str(number))
            editor.history.checkpoint()
            await pilot.press("shift+left")
            assert editor.selected_text == str(number)
        assert tabs.tab_count == 3
        await select_tab(tabs, "query-1", pilot)
        first = tabs.get_pane("query-1").query_one(TextArea)
        first.focus()
        assert first.text == "SELECT 1"
        assert first.selected_text == "1"
        await pilot.press("ctrl+z")
        assert first.text == ""
        await pilot.press("ctrl+y")
        assert first.text == "SELECT 1"
        assert editor.text == "SELECT 2"


async def test_execution_replaces_result_in_same_workspace(small_parquet: Path) -> None:
    from textual.coordinate import Coordinate

    from parqx.tui.widgets import ArrowTable
    from tests.helpers import open_query

    app = Parqx([small_parquet])
    async with app.run_test(size=(120, 40)) as pilot:
        tabs = app.query_one(TabbedContent)
        await wait_for(lambda: not tabs.get_pane("source-1").loading, pilot)
        query = await open_query(app, pilot)
        assert query.run_button.disabled
        query.editor.load_text("SELECT name FROM smoke ORDER BY id")
        await pilot.press("ctrl+enter")
        await wait_for(lambda: bool(query.query(ArrowTable)), pilot)
        table = query.query_one(ArrowTable)
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == "alice"
        assert table.row_count == 5
        assert query.editor.has_focus
        query.editor.load_text("SELECT count(*) AS total FROM smoke")
        query.editor.move_cursor((0, 7))
        await pilot.press("shift+right")
        assert query.editor.selected_text == "c"
        table.focus()
        await pilot.press("ctrl+enter")
        await wait_for(lambda: table.columns[0].name == "total", pilot)
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == 5
        assert table.row_count == 1
        assert table.has_focus
        query.editor.load_text("SELECT 7 AS from_tab_strip")
        await select_tab(tabs, "query-1", pilot)
        await pilot.press("ctrl+enter")
        await wait_for(lambda: table.columns[0].name == "from_tab_strip", pilot)
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == 7
        query.editor.load_text("SELECT 9 AS from_button")
        await wait_for(lambda: not query.run_button.disabled, pilot)
        assert await pilot.click(query.run_button)
        await wait_for(
            lambda: table.columns[0].name == "from_button" and not query.running, pilot
        )
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == 9
        assert not query.run_button.disabled
        assert tabs.tab_count == 2
        assert len(app.catalog.snapshot()) == 1


@pytest.mark.parametrize("fail", [False, True])
async def test_completion_never_moves_focus_from_another_workspace(
    small_parquet: Path, fail: bool
) -> None:
    from threading import Event
    from unittest.mock import patch

    import duckdb
    from textual.coordinate import Coordinate

    from parqx.tui.widgets import ArrowTable
    from tests.helpers import open_query

    started, release = Event(), Event()
    original_sql = duckdb.DuckDBPyConnection.sql

    def delayed_sql(
        connection: duckdb.DuckDBPyConnection, sql: str
    ) -> duckdb.DuckDBPyRelation:
        started.set()
        assert release.wait(timeout=15)
        if fail and sql == "SELECT 42 AS answer":
            raise RuntimeError("failed background query")
        return original_sql(connection, sql)

    app = Parqx([small_parquet])
    with patch.object(duckdb.DuckDBPyConnection, "sql", delayed_sql):
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                tabs = app.query_one(TabbedContent)
                first = await open_query(app, pilot)
                first.editor.load_text("SELECT 42 AS answer")
                await pilot.press("ctrl+enter")
                await wait_for(started.is_set, pilot)
                await pilot.press("ctrl+enter", "x", "enter", "backspace", "ctrl+w")
                assert first.editor.text == "SELECT 42 AS answer"
                assert first.editor.read_only
                assert tabs.active == "query-1"
                second = await open_query(app, pilot)
                second.editor.load_text("SELECT 17")
                await pilot.press("ctrl+enter")
                assert not second.editor.read_only
                second.editor.move_cursor((0, 9))
                await pilot.press("space", "-", "-", "space", "x")
                assert second.editor.text == "SELECT 17 -- x"
                release.set()
                await wait_for(lambda: not first.running, pilot)
                if fail:
                    assert not first.query(ArrowTable)
                else:
                    assert (
                        first.query_one(ArrowTable)
                        .get_cell_at(Coordinate(0, 0))
                        .as_py()
                        == 42
                    )
                assert tabs.active == "query-2"
                assert second.editor.has_focus
                assert not first.editor.read_only
                assert not second.query(ArrowTable)
                await pilot.press("ctrl+enter")
                await wait_for(lambda: bool(second.query(ArrowTable)), pilot)
                assert (
                    second.query_one(ArrowTable).get_cell_at(Coordinate(0, 0)).as_py()
                    == 17
                )
            finally:
                release.set()


async def test_closed_workspace_releases_result_while_table_reference_survives(
    small_parquet: Path,
) -> None:
    import gc
    import weakref

    from tests.helpers import run_query

    app = Parqx([small_parquet])
    async with app.run_test() as pilot:
        table = await run_query(app, pilot, "SELECT * FROM smoke")
        data = weakref.ref(table.data)
        tabs = app.query_one(TabbedContent)
        await pilot.press("ctrl+w")
        await confirm_query_close(app, pilot)
        await wait_for(lambda: tabs.tab_count == 1, pilot)

        def released() -> bool:
            gc.collect()
            return data() is None

        await wait_for(released, pilot)
        assert table.row_count == 0
