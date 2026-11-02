"""Explicit value selection through the real file and Query interfaces."""

import gc
import weakref
from pathlib import Path

import pyarrow as pa
import pytest
from textual.coordinate import Coordinate
from textual.widget import Widget
from textual.widgets import Static, TabbedContent, TextArea

from parqx.tui.app import Parqx
from parqx.tui.widgets import ArrowTable
from parqx.tui.widgets.arrow_table import CursorType
from parqx.tui.widgets.value_browser import ValueBrowser
from tests.helpers import (
    confirm_query_close,
    open_query,
    run_query,
    select_tab,
    wait_for,
    wait_for_source_table,
)


@pytest.mark.parametrize("query_result", [False, True], ids=["file", "query"])
async def test_enter_reads_complete_cell_without_following_cursor(
    scalar_parquet: Path, query_result: bool
) -> None:
    app = Parqx([scalar_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        if query_result:
            query = await open_query(app, pilot)
            query.editor.load_text("SELECT * FROM scalars")
            await pilot.press("ctrl+enter")
            await wait_for(lambda: query.table is not None and not query.running, pilot)
            assert query.table is not None
            table = query.table
        table.focus()
        assert isinstance(table.parent, Widget)
        browser = table.parent.query_one(".value-browser")
        reader = browser.query_one(".value-reader", TextArea)
        assert not browser.display

        await pilot.press("enter")
        assert browser.display
        assert table.has_focus
        assert reader.text == "[bold]literal[/bold] 中文 👋\n" + "read me\n" * 60
        assert browser.region.width == pytest.approx(
            table.parent.region.width * 0.36, abs=1
        )
        assert browser.region.y == table.region.y
        assert browser.region.height == table.region.height

        await pilot.press("down", "right")
        assert table.cursor_coordinate == Coordinate(1, 1)
        assert reader.text.startswith("[bold]literal[/bold] 中文 👋\n")
        # Repeated mouse clicks only position the table cursor.
        await pilot.click(table, offset=(8, 2))
        await pilot.click(table, offset=(8, 2))
        assert table.cursor_coordinate == Coordinate(1, 0)
        assert table.has_focus
        assert reader.text.startswith("[bold]literal[/bold] 中文 👋\n")
        table.move_cursor(row=0, column=2)
        await pilot.press("enter")
        assert reader.text == "18446744073709551615"
        assert table.has_focus
        assert isinstance(table.get_cell_at(Coordinate(0, 2)), pa.Scalar)


async def test_visibility_resumes_reading_and_table_focus(scalar_parquet: Path) -> None:
    app = Parqx([scalar_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        assert table.parent is not None
        browser = table.parent.query_one(".value-browser")
        reader = browser.query_one(".value-reader", TextArea)
        table.focus()
        await pilot.press("v")
        assert browser.display
        assert table.has_focus
        assert reader.text == "Press Enter in the table to select a cell or row."
        await pilot.press("tab")
        assert reader.has_focus
        await pilot.press("backspace")
        assert table.has_focus

        await pilot.press("enter", "tab", "pagedown")
        assert reader.has_focus
        assert reader.scroll_y > 0
        position = reader.scroll_y
        text = reader.text
        await pilot.press("enter")
        assert reader.text == text
        await pilot.press("escape")
        assert table.has_focus
        assert browser.display
        await pilot.press("down", "v")
        assert not browser.display
        await pilot.press("v", "tab")
        assert reader.has_focus
        assert reader.text == text
        assert reader.scroll_y == position
        await pilot.press("v")
        assert not browser.display
        assert table.has_focus
        await pilot.press("v")
        assert reader.scroll_y == position
        table.move_cursor(row=0, column=0)
        await pilot.press("enter")
        assert table.has_focus
        assert reader.scroll_y == 0


async def test_query_input_and_successful_replacement(scalar_parquet: Path) -> None:
    app = Parqx([scalar_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        query = await open_query(app, pilot)
        query.editor.load_text("SELECT * FROM scalars")
        await pilot.press("ctrl+enter")
        await wait_for(lambda: query.table is not None and not query.running, pilot)
        table = query.table
        assert table is not None
        assert isinstance(table.parent, Widget)
        browser = table.parent.query_one(".value-browser")
        reader = browser.query_one(".value-reader", TextArea)
        table.focus()
        await pilot.press("enter", "tab", "pagedown")
        assert reader.has_focus
        assert reader.scroll_y > 0
        assert query.editor.region.width == table.parent.region.width
        assert query.run_button.region.bottom <= browser.region.y
        selected_text = reader.text

        query.editor.focus()
        query.editor.move_cursor(query.editor.document.end)
        await pilot.press("v", "tab", "backspace")
        assert query.editor.has_focus
        assert query.editor.text.startswith("SELECT * FROM scalarsv")
        assert reader.text == selected_text
        assert browser.display
        query.editor.load_text("SELECT 'new result' AS note")
        reader.focus()
        await pilot.press("ctrl+enter")
        await wait_for(lambda: table.column_count == 1 and not query.running, pilot)
        assert reader.has_focus
        assert browser.display
        assert reader.text == "Press Enter in the table to select a cell or row."
        assert reader.scroll_y == 0
        await pilot.press("escape", "enter")
        assert reader.text == "new result"
        query.editor.load_text("SELECT 'no rows' AS note WHERE false")
        await pilot.press("ctrl+enter")
        await wait_for(lambda: table.row_count == 0 and not query.running, pilot)
        assert reader.text == "No rows to inspect."
        assert table.has_focus
        await pilot.press("enter", "v", "v")
        assert reader.text == "No rows to inspect."


async def test_views_keep_independent_selections_and_release_on_close(
    scalar_parquet: Path,
) -> None:
    app = Parqx([scalar_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        tabs = app.query_one(TabbedContent)
        source_browser = tabs.get_pane("source-1").query_one(ValueBrowser)
        table.focus()
        await pilot.press("enter", "tab", "pagedown")
        position = source_browser.reader.scroll_y
        assert source_browser.root is not None
        source_value = weakref.ref(source_browser.root.value)
        query = await open_query(app, pilot)
        query.editor.load_text("SELECT true AS enabled")
        await pilot.press("ctrl+enter")
        await wait_for(lambda: query.table is not None and not query.running, pilot)
        browser = query.query_one(ValueBrowser)
        assert not browser.display
        assert query.table is not None
        query.table.focus()
        await pilot.press("enter")
        assert browser.reader.text == "true"
        assert browser.root is not None
        query_value = weakref.ref(browser.root.value)
        await select_tab(tabs, "source-1", pilot)
        assert source_browser.display
        assert source_browser.reader.text.startswith("[bold]literal[/bold]")
        assert source_browser.reader.scroll_y == position
        table.focus()
        await pilot.press("ctrl+w")
        await wait_for(lambda: tabs.tab_count == 1, pilot)
        assert browser.display
        assert browser.reader.text == "true"
        await pilot.press("ctrl+w")
        await confirm_query_close(app, pilot)
        await wait_for(lambda: tabs.tab_count == 0, pilot)

        def released() -> bool:
            gc.collect()
            return source_value() is None and query_value() is None

        await wait_for(released, pilot)


async def test_unsupported_rows_and_scalar_states_are_explicit(
    scalar_parquet: Path,
) -> None:
    app = Parqx([scalar_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        assert table.parent is not None
        browser = table.parent.query_one(".value-browser")
        reader = browser.query_one(".value-reader", TextArea)
        info = browser.query_one(".value-info", Static)
        table.focus()
        table.move_cursor(row=1, column=0)
        await pilot.press("enter")
        assert reader.text == '""'
        assert "Empty string" in str(info.content)
        table.move_cursor(row=2, column=0)
        await pilot.press("enter")
        assert reader.text == "null"
        assert "Complete value" in str(info.content)
        table.move_cursor(row=2, column=1)
        await pilot.press("enter")
        assert reader.text == "null"
        assert "Null value" in str(info.content)
        table.move_cursor(row=0, column=3)
        await pilot.press("enter")
        assert reader.text == "true"
        table.move_cursor(row=0, column=4)
        await pilot.press("enter")
        assert "Unsupported" in reader.text
        assert "list" in str(browser.query_one(".value-type", Static).content)
        table.cursor_type = "row"
        await pilot.press("enter")
        assert "Row browsing" in reader.text
        assert "Unsupported" in str(info.content)

        modes: tuple[CursorType, ...] = ("column", "none")
        for mode in modes:
            table.cursor_type = mode
            await pilot.press("v")
            assert not browser.display
            await pilot.press("enter")
            assert not browser.display
            assert "Row browsing" in reader.text
            await pilot.press("v")


async def test_initial_empty_result_has_no_value_to_select(small_parquet: Path) -> None:
    app = Parqx([small_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        query = await open_query(app, pilot)
        query.editor.load_text("SELECT 42 AS value WHERE false")
        await pilot.press("ctrl+enter")
        await wait_for(lambda: query.table is not None and not query.running, pilot)
        table = query.table
        assert table is not None
        assert table.parent is not None
        browser = table.parent.query_one(".value-browser")
        reader = browser.query_one(".value-reader", TextArea)
        table.focus()
        await pilot.press("enter")
        assert not browser.display
        await pilot.press("v", "tab")
        assert reader.has_focus
        assert reader.text == "No rows to inspect."


async def test_reading_a_value_preserves_the_sql_editor_viewport(
    small_parquet: Path,
) -> None:
    app = Parqx([small_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await run_query(app, pilot, "SELECT name FROM smoke")
        query = app.query_one("#query-1")
        editor = query.query_one(".sql-draft", TextArea)
        editor.load_text("SELECT name FROM smoke" + "\n-- a longer SQL draft" * 30)
        await pilot.pause()
        await wait_for(lambda: editor.max_scroll_y > 20, pilot)
        editor.scroll_to(y=20, animate=False, force=True)
        await pilot.pause()
        position = editor.scroll_y
        assert position > 0
        await pilot.press("enter")
        assert query.query_one(".value-reader", TextArea).text == "alice"
        assert table.has_focus
        assert editor.scroll_y == position
