import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from textual.pilot import Pilot
from textual.widgets import TextArea

from parqx.query.engine import QueryLimits
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable


async def wait_for(predicate: Callable[[], bool], pilot: Pilot[Any]) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await pilot.pause()


async def test_initial_sql_bypasses_full_source_read(small_parquet: Path) -> None:
    app = ParqxApp(small_parquet, initial_sql="SELECT name FROM data WHERE id = 3")
    with patch("pyarrow.parquet.read_table", side_effect=AssertionError("full read")):
        async with app.run_test(size=(100, 32)) as pilot:
            await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
            table = app.query_one(ArrowTable)
            assert table.row_count == 1
            assert table.columns[0].name == "name"
            assert table.size.height >= 10
            assert app.query_error is None


async def test_query_replace_error_empty_and_browse(small_parquet: Path) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test(size=(100, 32)) as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        await pilot.press("f2")
        editor = app.query_one(TextArea)
        editor.load_text("SELECT id FROM data WHERE id > 3")
        await pilot.press("f5")
        await wait_for(lambda: not app.query_running, pilot)
        table = app.query_one(ArrowTable)
        assert (table.row_count, table.column_count) == (2, 1)
        editor.load_text("SELECT missing FROM data")
        await pilot.press("f5")
        await wait_for(lambda: app.query_error is not None, pilot)
        assert table.row_count == 2
        editor.load_text("SELECT name FROM data WHERE false")
        await pilot.press("f5")
        await wait_for(lambda: not app.query_running, pilot)
        assert table.row_count == 0
        assert table.columns[0].name == "name"
        await pilot.click("#browse-file")
        await wait_for(lambda: table.row_count == 5, pilot)
        assert table.column_count == 3


async def test_preview_and_new_query_after_cancel(small_parquet: Path) -> None:
    app = ParqxApp(
        small_parquet,
        initial_sql="SELECT * FROM data",
        query_limits=QueryLimits(preview_rows=2),
    )
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        assert app.query_one(ArrowTable).row_count == 2
        editor = app.query_one(TextArea)
        editor.load_text("SELECT sum(sin(i)) FROM range(1000000000) t(i)")
        await pilot.press("f5")
        await pilot.press("escape")
        assert not app.query_running
        editor.load_text("SELECT 42 AS answer")
        await pilot.press("f5")
        await wait_for(lambda: not app.query_running, pilot)
        assert app.query_error is None
        table = app.query_one(ArrowTable)
        assert table.row_count == 1
        assert table.columns[0].name == "answer"


async def test_editor_text_does_not_trigger_table_bindings(small_parquet: Path) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        await pilot.press("f2", "h", "i", "z", "c")
        table = app.query_one(ArrowTable)
        assert table.show_header
        assert table.show_row_index
        assert not table.zebra_stripes
        assert table.cursor_type == "cell"
