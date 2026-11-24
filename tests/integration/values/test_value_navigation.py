"""Navigate available row and container values through the production app."""

from pathlib import Path

import pyarrow as pa
from textual.widgets import Button, Static, TabbedContent

from parqx.query import QueryResult
from parqx.tui.app import Parqx
from parqx.tui.values.browser import ValueBrowser
from parqx.tui.values.children import ValueChildren
from parqx.tui.values.reader import ScalarReader
from tests.helpers import open_query, run_query, wait_for_source_table


async def test_row_struct_and_scalar_share_one_browser(nested_parquet: Path) -> None:
    app = Parqx([nested_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        assert table.parent is not None
        browser = table.parent.query_one(ValueBrowser)
        table.focus()
        table.cursor_type = "row"
        await pilot.press("enter")
        assert browser.display
        assert table.has_focus
        options = browser.query_one(ValueChildren)
        assert "16 fields" in str(browser.query_one(".value-type", Static).content)

        await pilot.press("tab", "down", "enter")
        assert options.has_focus
        assert "2 fields" in str(browser.metadata.content)
        assert '"name"' in options.render_line(1).text
        await pilot.press("enter")
        reader = browser.query_one(ScalarReader)
        assert reader.has_focus
        assert reader.text == "Ada\n[bold]中文[/bold]"
        await pilot.press("backspace")
        assert options.has_focus
        assert options.highlighted == 0
        await pilot.press("backspace")
        assert options.has_focus
        assert options.highlighted == 1
        await pilot.press("escape")
        assert table.has_focus
        assert browser.display


async def test_list_map_mouse_navigation_and_breadcrumb_return(
    nested_parquet: Path,
) -> None:
    app = Parqx([nested_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        await wait_for_source_table(app, pilot)
        table = await run_query(app, pilot, "SELECT * FROM nested")
        assert table.parent is not None
        browser = table.parent.query_one(ValueBrowser)
        options = browser.query_one(ValueChildren)
        reader = browser.query_one(ScalarReader)
        tabs = app.query_one(TabbedContent)
        tab_count = tabs.tab_count
        table.focus()
        table.move_cursor(row=0, column=2)
        await pilot.press("enter", "tab")
        assert options.has_focus
        assert options.render_line(0).text.strip() == "["
        assert options.render_line(3).text.strip() == "]"
        for line in (0, 3):
            assert await pilot.click(options, offset=(2, line))
            assert options.has_focus
            assert options.highlighted == 0
            assert len(browser.query(".value-crumb")) == 1
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
        assert options.highlighted == 1
        assert "map" in str(browser.query_one(".value-type", Static).content)
        await pilot.press("escape", "v", "v", "tab")
        assert options.highlighted == 1
        assert tabs.tab_count == tab_count

        await pilot.press("escape")
        table.move_cursor(row=1, column=2)
        await pilot.press("enter", "tab", "enter")
        assert options.has_focus
        assert options.highlighted is None
        assert options.render_line(0).text.strip() == "[]"
        assert "0 items" in str(browser.query_one(".value-type", Static).content)
        await pilot.press("escape")
        table.move_cursor(row=2, column=2)
        await pilot.press("enter", "tab")
        assert reader.has_focus
        assert reader.text == "null"
        assert not options.display


async def test_parent_position_survives_hide_and_new_roots_reset_it(
    nested_parquet: Path,
) -> None:
    app = Parqx([nested_parquet])
    async with app.run_test(size=(140, 26)) as pilot:
        table = await wait_for_source_table(app, pilot)
        assert table.parent is not None
        browser = table.parent.query_one(ValueBrowser)
        options = browser.query_one(ValueChildren)
        table.focus()
        table.cursor_type = "row"
        await pilot.press("enter", "tab")
        options.scroll_to(y=options.max_scroll_y, animate=False)
        await pilot.pause()
        reading_position = options.scroll_y
        await pilot.press("v", "v", "tab")
        assert options.highlighted == 0
        assert options.scroll_y == reading_position
        await pilot.press("end")
        assert options.highlighted == 15
        position = options.scroll_y
        assert position > 0
        await pilot.press("enter", "v")
        assert table.has_focus
        table.move_cursor(row=1)
        await pilot.press("v", "tab")
        assert browser.reader.text == "11"
        assert len(browser.query(".value-crumb")) == 2
        await pilot.press("backspace")
        assert options.highlighted == 15
        assert options.scroll_y == position
        await pilot.press("home")
        assert options.highlighted == 0
        await pilot.press("backspace")
        assert table.has_focus
        # Enter confirms row 1 and replaces the old row 0 path and reading position.
        await pilot.press("enter", "tab")
        assert options.highlighted == 0
        assert options.scroll_y == 0
        assert len(browser.query(".value-crumb")) == 1


async def test_deep_unicode_breadcrumbs_and_field_tooltips(deep_parquet: Path) -> None:
    app = Parqx([deep_parquet])
    label = "非常长的字段名称_" * 6
    async with app.run_test(size=(140, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        assert table.parent is not None
        browser = table.parent.query_one(ValueBrowser)
        options = browser.query_one(ValueChildren)
        table.focus()
        await pilot.press("enter", "tab", "down")
        assert "…" in options.render_line(2).text
        assert options.render_line(3).text.startswith("      {")
        for y in (2, 3):
            line = options.render_line(y)
            assert all(
                segment.style is not None and segment.style.meta.get("value_child") == 1
                for segment in line
            )
        assert await pilot.hover(options, offset=(8, 2))
        assert label in str(options.tooltip)
        info = browser.query_one(".value-info", Static)
        assert "…\nstruct · 2 fields" in str(info.content)
        assert label in str(info.tooltip)
        for clicked_line in (2, 3):
            assert await pilot.click(options, offset=(8, clicked_line))
            crumbs = list(browser.query(".value-crumb").results(Button))
            assert len(crumbs) == 2
            assert str(crumbs[-1].tooltip) == label
            await pilot.press("backspace")
            assert options.highlighted == 1
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
        assert options.highlighted == 1
        assert len(browser.query(".value-crumb")) == 3
        await pilot.resize_terminal(120, 40)
        assert options.render_line(3).text.startswith("      {")
        assert options.max_scroll_x == 0
        await pilot.press("backspace", "backspace")
        assert options.highlighted == 1
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
        options = browser.query_one(ValueChildren)
        assert options.render_line(0).text.strip() == "{}"
        assert options.highlighted is None
        assert "0 fields" in str(browser.metadata.content)


async def test_old_child_messages_cannot_change_the_current_browse_path(
    nested_parquet: Path,
) -> None:
    app = Parqx([nested_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        assert table.parent is not None
        browser = table.parent.query_one(ValueBrowser)
        table.focus()
        table.cursor_type = "row"
        await pilot.press("enter", "tab")
        children = browser.options
        previous = children.document
        assert previous is not None
        await pilot.press("down", "enter")
        assert children.document is not previous
        children.post_message(ValueChildren.Highlighted(previous, 1))
        children.post_message(ValueChildren.Selected(previous, 1))
        await pilot.pause()
        assert children.has_focus
        assert children.highlighted == 0
        assert len(browser.query(".value-crumb")) == 2
        assert "name" in str(browser.info.content)
        await pilot.press("enter")
        assert browser.reader.text == "Ada\n[bold]中文[/bold]"
