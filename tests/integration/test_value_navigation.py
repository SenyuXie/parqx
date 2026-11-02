"""Navigate available row and container values through the production app."""

from pathlib import Path

import pyarrow as pa
import pytest
from textual.widgets import Button, OptionList, Static, TabbedContent, TextArea

from parqx.query.engine import QueryResult
from parqx.tui.app import Parqx
from parqx.tui.widgets.query_workspace import QueryWorkspace
from parqx.tui.widgets.value_browser import ValueBrowser
from tests.helpers import open_query, run_query, wait_for, wait_for_source_table


@pytest.mark.parametrize("query_result", [False, True], ids=["file", "query"])
async def test_row_struct_and_scalar_share_one_browser(
    nested_parquet: Path, query_result: bool
) -> None:
    app = Parqx([nested_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        if query_result:
            table = await run_query(app, pilot, "SELECT * FROM nested")
        assert table.parent is not None
        browser = table.parent.query_one(ValueBrowser)
        table.focus()
        table.cursor_type = "row"
        await pilot.press("enter")
        assert browser.display
        assert table.has_focus
        options = browser.query_one(OptionList)
        assert options.option_count == 18  # Sixteen columns and two delimiters.
        assert "extra_11" in str(options.get_option_at_index(16).prompt)
        assert "16 fields" in str(browser.query_one(".value-type", Static).content)

        await pilot.press("tab", "down", "enter")
        assert options.has_focus
        assert options.option_count == 4
        assert '"name"' in str(options.get_option_at_index(1).prompt)
        backgrounds = {
            segment.style.bgcolor
            for segment in options.render_line(1)
            if segment.text and segment.style
        }
        assert len(backgrounds) == 1
        await pilot.press("enter")
        reader = browser.query_one(TextArea)
        assert reader.has_focus
        assert reader.text == "Ada\n[bold]中文[/bold]"
        await pilot.press("backspace")
        assert options.has_focus
        assert options.highlighted == 1
        await pilot.press("backspace")
        assert options.has_focus
        assert options.highlighted == 2
        await pilot.press("escape")
        assert table.has_focus
        assert browser.display


@pytest.mark.parametrize("query_result", [False, True], ids=["file", "query"])
async def test_list_map_mouse_navigation_and_breadcrumb_return(
    nested_parquet: Path, query_result: bool
) -> None:
    app = Parqx([nested_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        if query_result:
            table = await run_query(app, pilot, "SELECT * FROM nested")
        assert table.parent is not None
        browser = table.parent.query_one(ValueBrowser)
        options = browser.query_one(OptionList)
        reader = browser.query_one(TextArea)
        tabs = app.query_one(TabbedContent)
        tab_count = tabs.tab_count
        table.focus()
        table.move_cursor(row=0, column=2)
        await pilot.press("enter", "tab")
        assert options.has_focus
        assert str(options.get_option_at_index(0).prompt).strip() == "["
        assert str(options.get_option_at_index(3).prompt).strip() == "]"
        assert await pilot.click(options, offset=(8, 1))
        await pilot.press("enter")
        assert reader.has_focus
        assert reader.text == "book"
        await pilot.press("backspace", "backspace", "escape")

        table.move_cursor(row=0, column=3)
        await pilot.press("enter", "tab", "down", "enter", "enter")
        assert reader.text == "9"
        await pilot.press("backspace", "down", "enter", "enter")
        assert reader.text == "north"
        assert reader.has_focus
        crumbs = list(browser.query(".value-crumb").results(Button))
        assert [str(button.tooltip) for button in crumbs] == [
            "Row 0 · attributes",
            "[1]",
            "value",
            "[0]",
        ]
        assert browser.crumbs.scroll_x > 0
        browser.crumbs.scroll_to(x=0, animate=False)
        await pilot.pause()
        assert await pilot.click(crumbs[0])
        assert options.has_focus
        assert options.highlighted == 2
        assert "map" in str(browser.query_one(".value-type", Static).content)
        await pilot.press("escape", "v", "v", "tab")
        assert options.highlighted == 2
        assert tabs.tab_count == tab_count

        await pilot.press("escape")
        table.move_cursor(row=1, column=2)
        await pilot.press("enter", "tab", "enter")
        assert options.has_focus
        assert options.highlighted is None
        assert options.option_count == 1
        assert str(options.get_option_at_index(0).prompt).strip() == "[]"
        assert "0 items" in str(browser.query_one(".value-type", Static).content)
        await pilot.press("escape")
        table.move_cursor(row=2, column=2)
        await pilot.press("enter", "tab")
        assert reader.has_focus
        assert reader.text == "null"
        assert not options.display


@pytest.mark.parametrize("query_result", [False, True], ids=["file", "query"])
async def test_parent_position_survives_hide_and_new_roots_reset_it(
    nested_parquet: Path, query_result: bool
) -> None:
    app = Parqx([nested_parquet])
    async with app.run_test(size=(140, 40 if query_result else 26)) as pilot:
        table = await wait_for_source_table(app, pilot)
        query = None
        if query_result:
            table = await run_query(app, pilot, "SELECT * FROM nested")
            query = app.query_one(QueryWorkspace)
        assert table.parent is not None
        browser = table.parent.query_one(ValueBrowser)
        options = browser.query_one(OptionList)
        table.focus()
        table.cursor_type = "row"
        await pilot.press("enter", "tab")
        options.scroll_to(y=options.max_scroll_y, animate=False)
        await pilot.pause()
        reading_position = options.scroll_y
        await pilot.press("v", "v", "tab")
        assert options.highlighted == 1
        assert options.scroll_y == reading_position
        await pilot.press("end")
        assert options.highlighted == 16
        position = options.scroll_y
        assert position > 0
        await pilot.press("enter", "v")
        assert table.has_focus
        table.move_cursor(row=1)
        await pilot.press("v", "tab")
        assert browser.reader.text == "11"
        assert len(browser.query(".value-crumb")) == 2
        await pilot.press("backspace")
        assert options.highlighted == 16
        assert options.scroll_y == position
        await pilot.press("home")
        assert options.highlighted == 1
        await pilot.press("backspace")
        assert table.has_focus
        # Enter confirms row 1 and replaces the old row 0 path and reading position.
        await pilot.press("enter", "tab")
        assert options.highlighted == 1
        assert options.scroll_y == 0
        assert len(browser.query(".value-crumb")) == 1
        await pilot.press("down", "enter")
        assert options.has_focus
        if query is not None:
            query.editor.load_text("SELECT * FROM nested")
            await pilot.press("ctrl+enter")
            await wait_for(lambda: not query.running and browser.root is None, pilot)
            assert browser.display
            assert browser.reader.has_focus
            assert len(browser.query(".value-crumb")) == 0
            assert "Press Enter" in browser.reader.text
            await pilot.press("escape", "enter")
            assert len(browser.query(".value-crumb")) == 1


async def test_deep_unicode_breadcrumbs_and_field_tooltips(deep_parquet: Path) -> None:
    app = Parqx([deep_parquet])
    label = "非常长的字段名称_" * 6
    async with app.run_test(size=(140, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        assert table.parent is not None
        browser = table.parent.query_one(ValueBrowser)
        options = browser.query_one(OptionList)
        table.focus()
        await pilot.press("enter", "tab", "down")
        prompt = str(options.get_option_at_index(2).prompt)
        assert "…" in prompt
        assert "\n      {" in prompt
        assert await pilot.hover(options, offset=(8, 2))
        assert label in str(options.tooltip)
        info = browser.query_one(".value-info", Static)
        assert "…\nstruct · 2 fields" in str(info.content)
        assert label in str(info.tooltip)
        for _ in range(4):
            await pilot.press("enter", "down")
        await pilot.press("enter")
        assert browser.reader.text == '{"this remains": "plain text"}'
        assert browser.reader.has_focus
        crumbs = list(browser.query(".value-crumb").results(Button))
        assert len(crumbs) == 6
        assert crumbs[-1].has_class("current")
        assert str(crumbs[-1].tooltip) == label
        assert "…" in str(crumbs[-1].label)
        assert browser.crumbs.scroll_x == browser.crumbs.max_scroll_x
        ancestor = crumbs[2]
        browser.crumbs.scroll_to_widget(ancestor, animate=False)
        await pilot.pause()
        assert await pilot.click(ancestor)
        assert options.has_focus
        assert options.highlighted == 2
        assert len(browser.query(".value-crumb")) == 3
        await pilot.resize_terminal(120, 40)
        assert "\n      {" in str(options.get_option_at_index(2).prompt)
        assert options.max_scroll_x == 0
        await pilot.press("backspace", "backspace")
        assert options.highlighted == 2
        assert len(browser.query(".value-crumb")) == 1


async def test_duplicate_positions_and_empty_structs_at_arrow_delivery(
    small_parquet: Path,
) -> None:
    # DuckDB normalizes duplicate field names and rejects empty Struct values.
    # Deliver Arrow directly at the existing result boundary for those shapes.
    fields = pa.array(
        [("first", 7)], type=pa.struct([("x", pa.string()), ("x", pa.int64())])
    )
    data = pa.Table.from_batches(
        [
            pa.RecordBatch.from_arrays(
                [fields, pa.array(["second"]), pa.array([{}], type=pa.struct([]))],
                names=["same", "same", "empty"],
            )
        ]
    )
    app = Parqx([small_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        await wait_for_source_table(app, pilot)
        query = await open_query(app, pilot)
        await query.show_result(QueryResult("Arrow fixture", data, 0))
        assert query.table is not None
        assert query.view is not None
        table, browser = query.table, query.view.browser
        table.focus()
        table.cursor_type = "row"
        await pilot.press("enter", "tab", "enter", "down", "enter")
        assert browser.reader.text == "7"
        await pilot.press("backspace", "backspace", "down", "enter")
        assert browser.reader.text == "second"
        await pilot.press("backspace", "down", "enter")
        options = browser.query_one(OptionList)
        assert options.option_count == 1
        assert str(options.get_option_at_index(0).prompt).strip() == "{}"
        assert options.get_option_at_index(0).disabled
        assert options.highlighted is None
        assert "0 fields" in str(browser.metadata.content)
