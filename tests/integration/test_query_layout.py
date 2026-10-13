"""Panel allocation and native focus through the mounted application."""

from pathlib import Path

import pytest
from textual.widget import Widget
from textual.widgets import Button, TabbedContent

from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from tests.helpers import open_query, open_sql_query, select_tab, wait_for


async def test_editor_size_and_collapse_are_retained_per_workspace(
    small_parquet: Path,
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        first = await open_query(app, pilot)
        first.editor.load_text("SELECT * FROM smoke")
        await pilot.press("ctrl+enter")
        await wait_for(lambda: first.table is not None and not first.running, pilot)
        assert first.table is not None
        assert first.editor.content_size.height == 8
        initial_height = first.editor.region.height
        result_height = first.table.region.height
        assert await pilot.click(first.query_one(".shrink-editor", Button))
        await wait_for(lambda: first.editor.region.height < initial_height, pilot)
        smaller_height = first.editor.region.height
        assert first.table.region.height > result_height
        assert await pilot.click(first.query_one(".toggle-editor", Button))
        await wait_for(lambda: not first.editor.display, pilot)
        assert first.table.region.height > result_height + 3
        assert first.editor.text == "SELECT * FROM smoke"
        assert first.table.row_count == 5

        second = await open_query(app, pilot)
        assert second.editor.display
        assert second.editor.region.height == initial_height
        tabs = app.query_one(TabbedContent)
        await select_tab(tabs, "query-1", pilot)
        assert not first.editor.display
        assert await pilot.click(first.query_one(".toggle-editor", Button))
        await wait_for(lambda: first.editor.display, pilot)
        assert first.editor.region.height == smaller_height
        assert await pilot.click(first.query_one(".grow-editor", Button))
        await wait_for(lambda: first.editor.region.height == initial_height, pilot)
        assert first.editor.text == "SELECT * FROM smoke"
        assert first.table.row_count == 5


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
@pytest.mark.parametrize("show_sources", [False, True])
async def test_native_maximize_targets_editor_and_result_and_restores_layout(
    small_parquet: Path, size: tuple[int, int], show_sources: bool
) -> None:
    from textual.command import CommandList, CommandPalette
    from textual.widgets import Input

    app = ParqxApp([small_parquet])
    async with app.run_test(size=size) as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        query = await open_query(app, pilot)
        query.editor.load_text("SELECT * FROM smoke")
        await pilot.press("ctrl+enter")
        await wait_for(lambda: query.table is not None and not query.running, pilot)
        assert query.table is not None
        if show_sources:
            app.action_toggle_sources()
        await pilot.pause()
        editor_region = query.editor.region
        table_region = query.table.region
        query.editor.focus()
        await pilot.press("ctrl+p")
        assert isinstance(app.screen, CommandPalette)
        app.screen.query_one(Input).value = "Maximize"
        commands = app.screen.query_one(CommandList)
        await wait_for(lambda: commands.option_count == 1, pilot)
        await pilot.press("enter")
        await wait_for(lambda: not app.screen.is_modal, pilot)
        assert query.editor.is_maximized
        assert query.editor.region.height > editor_region.height
        assert query.editor.region.width == size[0]
        await pilot.press("escape")
        await wait_for(lambda: app.screen.maximized is None, pilot)
        await wait_for(
            lambda: (
                query.editor.region == editor_region
                and query.table is not None
                and query.table.region == table_region
            ),
            pilot,
        )
        assert query.editor.has_focus

        query.table.focus()
        await pilot.press("ctrl+p")
        assert isinstance(app.screen, CommandPalette)
        app.screen.query_one(Input).value = "Maximize"
        commands = app.screen.query_one(CommandList)
        await wait_for(lambda: commands.option_count == 1, pilot)
        await pilot.press("enter")
        await wait_for(lambda: not app.screen.is_modal, pilot)
        assert query.table.is_maximized
        assert query.table.region.height > table_region.height
        assert query.table.region.width == size[0]
        await pilot.press("ctrl+p")
        assert isinstance(app.screen, CommandPalette)
        app.screen.query_one(Input).value = "Minimize"
        commands = app.screen.query_one(CommandList)
        await wait_for(lambda: commands.option_count == 1, pilot)
        await pilot.press("enter")
        await wait_for(lambda: not app.screen.is_modal, pilot)
        assert app.screen.maximized is None
        await wait_for(
            lambda: (
                query.editor.region == editor_region
                and query.table is not None
                and query.table.region == table_region
            ),
            pilot,
        )
        assert query.table.has_focus
        assert query.editor.text == "SELECT * FROM smoke"
        assert query.table.row_count == 5


@pytest.mark.parametrize("target", ["editor", "result"])
@pytest.mark.parametrize("last_tab", [False, True])
async def test_closing_maximized_workspace_restores_main_view(
    small_parquet: Path, target: str, last_tab: bool
) -> None:
    from tests.helpers import confirm_query_close

    app = ParqxApp([small_parquet])
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        tabs = app.query_one(TabbedContent)
        if last_tab:
            await pilot.press("ctrl+w")
            await wait_for(lambda: tabs.tab_count == 0, pilot)
        query = await open_query(app, pilot)
        query.editor.load_text("SELECT * FROM smoke")
        await pilot.press("ctrl+enter")
        await wait_for(lambda: query.table is not None and not query.running, pilot)
        assert query.table is not None
        widget = query.editor if target == "editor" else query.table
        widget.focus()
        await pilot.pause()
        app.screen.action_maximize()
        await wait_for(lambda: app.screen.maximized is widget, pilot)
        await pilot.press("ctrl+w")
        await wait_for(lambda: app.screen.is_modal, pilot)
        await pilot.press("escape")
        await wait_for(lambda: not app.screen.is_modal, pilot)
        assert app.screen.maximized is widget
        assert widget.has_focus
        assert query.editor.text == "SELECT * FROM smoke"
        await pilot.press("ctrl+w")
        await confirm_query_close(app, pilot)
        await wait_for(lambda: tabs.tab_count == (0 if last_tab else 1), pilot)
        assert app.screen.maximized is None
        if last_tab:
            new_query = app.query_one("#new-query", Button)
            await wait_for(lambda: new_query.has_focus, pilot)
            assert app.screen.region.contains_region(new_query.region)
            assert await pilot.click(new_query)
            await wait_for(lambda: tabs.tab_count == 1, pilot)
            assert tabs.active == "query-2"
            return
        assert tabs.active == "source-1"
        pane = tabs.active_pane
        assert pane is not None
        source_table = pane.query_one(ArrowTable)
        await wait_for(lambda: source_table.has_focus, pilot)
        assert source_table.row_count == 5
        assert app.screen.region.contains_region(source_table.region)


async def test_long_sql_and_wide_results_keep_controls_reachable_after_resize(
    small_parquet: Path,
) -> None:
    from textual.widgets import Tabs

    from parqx.tui.widgets.sources_tree import SourcesTree

    app = ParqxApp([small_parquet])
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        query = await open_query(app, pilot)
        sql = (
            "SELECT repeat('wide value ', 25) AS wide_name, "
            "repeat('more data ', 25) AS more_data, "
            "repeat('other data ', 25) AS other_data, id, name FROM smoke"
            + "\n-- another SQL line"
            * 39
        )
        query.editor.load_text(sql)
        await pilot.press("ctrl+enter")
        await wait_for(lambda: query.table is not None and not query.running, pilot)
        table = query.table
        assert table is not None
        query.editor.move_cursor((39, 3))
        await pilot.press("shift+right")
        selection = query.editor.selection
        initial_height = query.editor.region.height
        assert await pilot.click(query.query_one(".grow-editor", Button))
        await wait_for(lambda: query.editor.region.height > initial_height, pilot)
        grown_height = query.editor.region.height
        details = query.query_one(".result-details", Button)
        toggle = query.query_one(".toggle-editor", Button)
        toggle.active_effect_duration = 0

        for width, height in ((80, 24), (60, 18), (120, 40)):
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            for _ in range(2):
                app.action_toggle_sources()
                await pilot.pause()
                assert query.editor.region.height <= grown_height
                assert query.editor.scroll_y > 0
                assert table.region.height >= 4
                assert table.max_scroll_x > 0
                assert "Result details" in details.render_line(0).text
                assert "Hide SQL" in toggle.render_line(0).text
                for control in query.query(Button):
                    assert app.screen.region.contains_region(control.region)
                assert query.editor.selection == selection
                assert query.editor.text == sql
                assert table.row_count == 5
            assert await pilot.click(toggle)
            await wait_for(lambda: not query.editor.display, pilot)
            assert "Show SQL" in toggle.render_line(0).text
            assert await pilot.click(toggle)
            await wait_for(lambda: query.editor.display, pilot)
            assert query.editor.selection == selection
        assert query.editor.region.height == grown_height
        table.focus()
        await pilot.press("end")
        assert table.scroll_x > 0
        app.action_toggle_sources()
        query.editor.focus()
        await pilot.pause()
        visited: set[Widget | None] = set()
        for _ in range(10):
            await pilot.press("shift+tab")
            visited.add(app.focused)
            if isinstance(app.focused, SourcesTree):
                break
        assert any(isinstance(widget, Tabs) for widget in visited)
        assert any(isinstance(widget, SourcesTree) for widget in visited)
        assert query.editor.text == sql
        shrink = query.query_one(".shrink-editor", Button)
        shrink.active_effect_duration = 0
        while not shrink.disabled:
            assert await pilot.click(shrink)
            await pilot.pause()
        assert query.editor.scrollable_content_region.height >= 1
        assert table.region.height >= 4


async def test_query_palette_reveals_new_workspace_from_maximized_editor(
    small_parquet: Path,
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        first = await open_query(app, pilot)
        first.editor.load_text("SELECT 42")
        app.screen.action_maximize()
        await wait_for(lambda: first.editor.is_maximized, pilot)
        query = await open_sql_query(pilot)
        tabs = app.query_one(TabbedContent)
        assert tabs.active == "query-2"
        assert app.screen.maximized is None
        editor = query.editor
        await wait_for(lambda: editor.region.height > 0, pilot)
        assert editor.text == ""
        assert first.editor.text == "SELECT 42"
        assert app.screen.region.contains_region(editor.region)
