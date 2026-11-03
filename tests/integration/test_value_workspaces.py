"""Value inspection through query, tab, and native layout transitions."""

from pathlib import Path

import pytest

from parqx.tui.app import Parqx
from parqx.tui.widgets.table_view import TableView
from tests.helpers import run_query, wait_for, wait_for_source_table


@pytest.mark.parametrize("query_result", [False, True], ids=["file", "query"])
@pytest.mark.parametrize("focus", ["table", "container", "reader"])
async def test_native_maximize_keeps_table_and_nested_browser_together(
    paged_values_parquet: Path, query_result: bool, focus: str
) -> None:
    app = Parqx([paged_values_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        if query_result:
            table = await run_query(app, pilot, "SELECT * FROM paged_values LIMIT 2")
        view = table.parent
        assert isinstance(view, TableView)
        browser = view.browser
        table.cursor_type = "row"
        table.focus()
        await pilot.press("enter", "tab", "end")
        if focus == "reader":
            await pilot.press("enter", "pagedown")
            assert browser.reader.text.startswith("row 0\n")
            assert browser.reader.scroll_y > 0
        elif focus == "table":
            await pilot.press("escape")
        focused = app.focused
        position = browser.reader.scroll_y
        root = browser.root
        geometry = (table.region, browser.region)

        app.screen.action_maximize()
        await pilot.pause()
        assert app.screen.maximized is view
        assert view.region.width == 140
        assert table.region.height == browser.region.height
        assert table.region.right == browser.region.x
        assert app.screen.region.contains_region(browser.region)
        assert app.focused is focused
        assert browser.root is root
        if focus == "reader":
            assert browser.reader.text.startswith("row 0\n")
            assert browser.reader.scroll_y == position
        else:
            assert browser.options.highlighted == 3

        app.screen.action_minimize()
        await wait_for(lambda: (table.region, browser.region) == geometry, pilot)
        assert app.focused is focused
        assert browser.root is root
        if focus == "reader":
            assert browser.reader.scroll_y == position
            await pilot.press("backspace")
            assert browser.options.highlighted == 3
        await pilot.press("escape", "v", "v", "tab")
        assert browser.options.has_focus
        assert browser.options.highlighted == 3


async def test_tabs_restore_independent_nested_paths_and_reading_positions(
    nested_parquet: Path,
) -> None:
    from textual.widgets import TabbedContent

    from tests.helpers import select_tab

    app = Parqx([nested_parquet])
    async with app.run_test(size=(140, 26)) as pilot:
        source_table = await wait_for_source_table(app, pilot)
        assert isinstance(source_table.parent, TableView)
        source_browser = source_table.parent.browser
        source_table.cursor_type = "row"
        source_table.focus()
        await pilot.press("enter", "tab", "end")
        source_position = source_browser.options.scroll_y
        assert source_position > 0
        await pilot.press("enter")
        assert source_browser.reader.text == "11"
        source_root = source_browser.root

        first_table = await run_query(
            app, pilot, "SELECT profile FROM nested WHERE id = 2"
        )
        assert isinstance(first_table.parent, TableView)
        first_browser = first_table.parent.browser
        await pilot.press("enter", "tab", "down", "enter")
        assert first_browser.reader.text == "false"
        first_root = first_browser.root

        second_table = await run_query(
            app, pilot, "SELECT {'note': repeat('query two\n', 100)} AS payload"
        )
        assert isinstance(second_table.parent, TableView)
        second_browser = second_table.parent.browser
        await pilot.press("enter", "tab", "enter", "pagedown")
        assert second_browser.reader.text == "query two\n" * 100
        second_position = second_browser.reader.scroll_y
        assert second_position > 0
        second_root = second_browser.root
        await pilot.press("v")
        tabs = app.query_one(TabbedContent)

        await select_tab(tabs, "source-1", pilot)
        assert source_browser.display
        assert source_browser.root is source_root
        assert source_browser.reader.text == "11"
        assert len(source_browser.query(".value-crumb")) == 2
        source_table.focus()
        await pilot.press("down", "tab", "backspace")
        assert source_browser.options.highlighted == 16
        assert source_browser.options.scroll_y == source_position

        await select_tab(tabs, "query-1", pilot)
        assert first_browser.display
        assert first_browser.root is first_root
        assert first_browser.reader.text == "false"
        first_table.focus()
        await pilot.press("tab", "backspace")
        assert first_browser.options.highlighted == 2

        await select_tab(tabs, "query-2", pilot)
        assert not second_browser.display
        second_table.focus()
        await pilot.press("v", "tab")
        assert second_browser.reader.has_focus
        assert second_browser.root is second_root
        assert second_browser.reader.text == "query two\n" * 100
        assert second_browser.reader.scroll_y == second_position
        assert len(second_browser.query(".value-crumb")) == 2
        await pilot.press("backspace")
        assert second_browser.options.highlighted == 1


@pytest.mark.parametrize("hidden", [False, True])
async def test_failed_execution_preserves_nested_inspection(
    paged_values_parquet: Path, hidden: bool
) -> None:
    from threading import Event
    from unittest.mock import patch

    import duckdb

    from parqx.tui.widgets.query_workspace import QueryWorkspace
    from tests.helpers import wait_for_query_error

    app = Parqx([paged_values_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await run_query(app, pilot, "SELECT * FROM paged_values LIMIT 2")
        query = app.query_one(QueryWorkspace)
        assert query.view is not None
        browser = query.view.browser
        table.cursor_type = "row"
        await pilot.press("enter", "tab", "end", "enter", "pagedown")
        root = browser.root
        position = browser.reader.scroll_y
        assert position > 0
        query.editor.load_text("SELECT missing_column FROM paged_values")
        await pilot.pause()
        assert browser.root is root
        assert browser.reader.scroll_y == position
        assert browser.reader.text.startswith("row 0\n")
        if hidden:
            await pilot.press("v")
        else:
            app.screen.action_maximize()
            await wait_for(lambda: app.screen.maximized is query.view, pilot)
        focused = app.focused
        started, release = Event(), Event()
        original_sql = duckdb.DuckDBPyConnection.sql

        def delayed_sql(
            connection: duckdb.DuckDBPyConnection, sql: str
        ) -> duckdb.DuckDBPyRelation:
            started.set()
            assert release.wait(timeout=15)
            return original_sql(connection, sql)

        with (
            patch.object(duckdb.DuckDBPyConnection, "sql", delayed_sql),
            patch.object(app, "notify", wraps=app.notify) as notify,
        ):
            try:
                await pilot.press("ctrl+enter")
                await wait_for(started.is_set, pilot)
                assert query.running
                assert browser.root is root
                assert browser.reader.scroll_y == position
                assert table.row_count == 2
                assert app.focused is focused
                await pilot.press("ctrl+w")
                assert not app.screen.is_modal
                assert "cannot be closed" in str(notify.call_args)
                assert browser.root is root
                notify.reset_mock()
                release.set()
                message = await wait_for_query_error(notify, query, pilot)
                assert "missing_column" in message
            finally:
                release.set()
        assert browser.display is not hidden
        assert browser.root is root
        assert browser.reader.scroll_y == position
        assert app.focused is focused
        assert len(browser.query(".value-crumb")) == 2
        if hidden:
            await pilot.press("v", "tab")
        else:
            assert app.screen.maximized is query.view
            app.screen.action_minimize()
            await pilot.pause()
        await pilot.press("backspace")
        assert browser.options.has_focus
        assert browser.options.highlighted == 3


@pytest.mark.parametrize(
    ("sql", "focus", "hidden", "empty"),
    [
        ("SELECT * FROM paged_values LIMIT 2", "container", False, False),
        ("SELECT * FROM paged_values LIMIT 2", "reader", False, False),
        ("SELECT 42 AS answer", "table", False, False),
        ("SELECT * FROM paged_values WHERE false", "reader", False, True),
        ("SELECT * FROM paged_values LIMIT 2", "table", True, False),
        ("SELECT 42 AS answer WHERE false", "editor", True, True),
    ],
    ids=[
        "same-schema-container",
        "same-schema-reader",
        "changed-schema-table",
        "empty-reader",
        "hidden-same-schema",
        "hidden-empty-editor",
    ],
)
async def test_success_invalidates_nested_state_without_changing_focus_region(
    paged_values_parquet: Path, sql: str, focus: str, hidden: bool, empty: bool
) -> None:
    from threading import Event
    from unittest.mock import patch

    import duckdb
    from textual.coordinate import Coordinate

    from parqx.tui.widgets.query_workspace import QueryWorkspace

    app = Parqx([paged_values_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await run_query(app, pilot, "SELECT * FROM paged_values LIMIT 2")
        query = app.query_one(QueryWorkspace)
        assert query.view is not None
        browser = query.view.browser
        table.cursor_type = "row"
        await pilot.press("enter", "tab")
        if focus == "container":
            await pilot.press("down", "enter", "down")
            assert browser.options.has_focus
            assert browser.options.highlighted == 2
        else:
            await pilot.press("end", "enter", "pagedown")
            assert browser.reader.scroll_y > 0
        assert len(browser.query(".value-crumb")) == 2
        previous_result = query.current_result
        query.editor.load_text(sql)
        if hidden:
            await pilot.press("v")
        if focus == "table":
            table.focus()
        elif focus == "editor":
            query.editor.focus()
        await pilot.pause()
        expected_focus = (
            browser.reader if focus in {"container", "reader"} else app.focused
        )
        started, release = Event(), Event()
        original_sql = duckdb.DuckDBPyConnection.sql

        def delayed_sql(
            connection: duckdb.DuckDBPyConnection, sql: str
        ) -> duckdb.DuckDBPyRelation:
            started.set()
            assert release.wait(timeout=15)
            return original_sql(connection, sql)

        with patch.object(duckdb.DuckDBPyConnection, "sql", delayed_sql):
            try:
                await pilot.press("ctrl+enter")
                await wait_for(started.is_set, pilot)
                assert query.current_result is previous_result
                assert browser.root is not None
                assert len(browser.query(".value-crumb")) == 2
                release.set()
                await wait_for(lambda: not query.running, pilot)
            finally:
                release.set()
        assert query.current_result is not previous_result
        assert browser.root is None
        assert not browser.query(".value-crumb")
        assert browser.options.option_count == 0
        assert browser.reader.scroll_y == 0
        assert browser.display is not hidden
        assert table.cursor_coordinate == Coordinate(0, 0)
        assert app.focused is expected_focus
        expected_text = (
            "No rows to inspect."
            if empty
            else "Press Enter in the table to select a cell or row."
        )
        assert browser.reader.text == expected_text
        if hidden:
            table.focus()
            await pilot.press("v", "tab")
            assert browser.reader.has_focus
            assert browser.root is None
            assert browser.reader.text == expected_text
        table.focus()
        await pilot.press("enter")
        assert (browser.root is None) is empty
        if not empty:
            assert len(browser.query(".value-crumb")) == 1
            assert browser.options.highlighted == 1
            assert browser.options.scroll_y == 0


@pytest.mark.parametrize("fail", [False, True])
async def test_background_queries_update_only_their_own_inspection(
    nested_parquet: Path, fail: bool
) -> None:
    from threading import Event
    from unittest.mock import patch

    import duckdb
    from textual.widgets import TabbedContent

    from parqx.tui.widgets.query_workspace import QueryWorkspace
    from tests.helpers import select_tab

    app = Parqx([nested_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        source_table = await wait_for_source_table(app, pilot)
        assert isinstance(source_table.parent, TableView)
        source_browser = source_table.parent.browser
        source_table.move_cursor(column=1)
        source_table.focus()
        await pilot.press("enter", "tab", "enter")
        assert source_browser.reader.text == "Ada\n[bold]中文[/bold]"
        source_root = source_browser.root

        first_table = await run_query(
            app, pilot, "SELECT profile FROM nested WHERE id = 2"
        )
        tabs = app.query_one(TabbedContent)
        first = tabs.active_pane
        assert isinstance(first, QueryWorkspace)
        assert first.view is not None
        first_browser = first.view.browser
        await pilot.press("enter", "tab", "down", "enter")
        first_root = first_browser.root
        assert first_browser.reader.text == "false"

        second_table = await run_query(
            app, pilot, "SELECT {'note': repeat('second query\n', 100)} AS payload"
        )
        second = tabs.active_pane
        assert isinstance(second, QueryWorkspace)
        assert second.view is not None
        second_browser = second.view.browser
        await pilot.press("enter", "tab", "enter", "pagedown")
        second_position = second_browser.reader.scroll_y
        assert second_position > 0
        second_root = second_browser.root

        first.editor.load_text(
            "SELECT missing_field FROM nested" if fail else "SELECT 42 AS answer"
        )
        second.editor.load_text("SELECT 7 AS answer WHERE false")
        original_sql = duckdb.DuckDBPyConnection.sql
        for running, active_id, active_reader in (
            (first, "query-2", second_browser.reader),
            (second, "source-1", source_browser.reader),
        ):
            assert running.id is not None
            await select_tab(tabs, running.id, pilot)
            running.editor.focus()
            started, release = Event(), Event()

            def delayed_sql(
                connection: duckdb.DuckDBPyConnection,
                sql: str,
                *,
                started: Event = started,
                release: Event = release,
            ) -> duckdb.DuckDBPyRelation:
                started.set()
                assert release.wait(timeout=15)
                return original_sql(connection, sql)

            def completed(query: QueryWorkspace = running) -> bool:
                return not query.running

            with patch.object(duckdb.DuckDBPyConnection, "sql", delayed_sql):
                try:
                    await pilot.press("ctrl+enter")
                    await wait_for(started.is_set, pilot)
                    await select_tab(tabs, active_id, pilot)
                    active_reader.focus()
                    await pilot.pause()
                    release.set()
                    await wait_for(completed, pilot)
                finally:
                    release.set()
            assert tabs.active == active_id
            assert active_reader.has_focus
            assert source_browser.root is source_root
            assert source_browser.reader.text == "Ada\n[bold]中文[/bold]"
            if running is first:
                assert second_browser.root is second_root
                assert second_browser.reader.scroll_y == second_position
                assert second_browser.reader.text == "second query\n" * 100
                if fail:
                    assert first_browser.root is first_root
                    assert first_browser.reader.text == "false"
                    assert len(first_browser.query(".value-crumb")) == 2
                else:
                    assert first_browser.root is None
                    assert "Press Enter" in first_browser.reader.text
        assert second_table.row_count == 0
        assert second_browser.root is None
        assert second_browser.display
        assert second_browser.reader.text == "No rows to inspect."
        await select_tab(tabs, "query-1", pilot)
        first_table.focus()
        await pilot.press("tab")
        assert first_browser.reader.has_focus
        await pilot.press("backspace")
        if fail:
            assert first_browser.options.highlighted == 2
        else:
            assert first_table.has_focus


@pytest.mark.parametrize("query_result", [False, True], ids=["file", "query"])
@pytest.mark.parametrize("focus", ["container", "reader"])
async def test_closing_maximized_inspection_releases_values_and_restores_workspace(
    nested_parquet: Path, query_result: bool, focus: str
) -> None:
    import gc
    import weakref

    from textual.widgets import TabbedContent

    from tests.helpers import confirm_query_close, select_tab

    app = Parqx([nested_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        source_table = await wait_for_source_table(app, pilot)
        source_table.cursor_type = "row"
        source_table.focus()
        await pilot.press("enter", "tab", "down", "enter", "down", "enter")
        query_table = await run_query(app, pilot, "SELECT * FROM nested WHERE id = 2")
        query_table.cursor_type = "row"
        await pilot.press("enter", "tab", "down", "enter", "down", "enter")
        table = query_table if query_result else source_table
        survivor = source_table if query_result else query_table
        assert isinstance(table.parent, TableView)
        assert isinstance(survivor.parent, TableView)
        view = table.parent
        browser = view.browser
        survivor_browser = survivor.parent.browser
        survivor_root = survivor_browser.root
        tabs = app.query_one(TabbedContent)
        pane_id = "query-1" if query_result else "source-1"
        await select_tab(tabs, pane_id, pilot)
        table.focus()
        await pilot.press("tab")
        assert browser.reader.text == ("false" if query_result else "true")
        if focus == "container":
            await pilot.press("backspace")
            assert browser.options.highlighted == 2
        focused = app.focused
        assert browser.root is not None
        value = weakref.ref(browser.root.value)
        data = weakref.ref(table.data)
        depth = len(browser.query(".value-crumb"))
        app.screen.action_maximize()
        await wait_for(lambda: app.screen.maximized is view, pilot)
        await pilot.press("ctrl+w")
        if query_result:
            await wait_for(lambda: app.screen.is_modal, pilot)
            await pilot.press("escape")
            await wait_for(lambda: not app.screen.is_modal, pilot)
            assert app.screen.maximized is view
            assert app.focused is focused
            assert len(browser.query(".value-crumb")) == depth
            assert value() is not None
            await pilot.press("ctrl+w")
            await confirm_query_close(app, pilot)
        await wait_for(lambda: tabs.tab_count == 1, pilot)
        assert not tabs.query(f"#{pane_id}")
        assert app.screen.maximized is None
        await wait_for(lambda: survivor.has_focus, pilot)
        assert app.screen.region.contains_region(survivor.region)
        assert survivor_browser.display
        assert survivor_browser.root is survivor_root
        await pilot.press("tab")
        assert survivor_browser.reader.has_focus
        assert survivor_browser.reader.text == ("true" if query_result else "false")
        await pilot.press("backspace")
        assert survivor_browser.options.highlighted == 2
        assert browser.root is None
        assert not browser.query(".value-crumb")
        assert browser.options.option_count == 0

        def released() -> bool:
            gc.collect()
            return value() is None and data() is None

        await wait_for(released, pilot)


@pytest.mark.parametrize("fail", [False, True])
async def test_shutdown_releases_inspection_before_pending_query_finishes(
    nested_parquet: Path, fail: bool
) -> None:
    import asyncio
    import gc
    import weakref
    from threading import Event
    from unittest.mock import patch

    import duckdb

    from parqx.tui.widgets.query_workspace import QueryWorkspace

    started, release = Event(), Event()
    original_sql = duckdb.DuckDBPyConnection.sql

    def delayed_sql(
        connection: duckdb.DuckDBPyConnection, sql: str
    ) -> duckdb.DuckDBPyRelation:
        if sql == "SELECT 42 AS delayed":
            started.set()
            assert release.wait(timeout=15)
            if fail:
                raise RuntimeError("late query failure")
        return original_sql(connection, sql)

    app = Parqx([nested_parquet])
    with (
        patch.object(duckdb.DuckDBPyConnection, "sql", delayed_sql),
        patch.object(app, "notify", wraps=app.notify) as notify,
    ):
        try:
            async with app.run_test(size=(140, 40)) as pilot:
                table = await run_query(app, pilot, "SELECT * FROM nested LIMIT 1")
                query = app.query_one(QueryWorkspace)
                assert query.view is not None
                browser = query.view.browser
                table.cursor_type = "row"
                await pilot.press("enter", "tab", "down", "enter", "enter")
                assert browser.root is not None
                value = weakref.ref(browser.root.value)
                data = weakref.ref(table.data)
                app.screen.action_maximize()
                await wait_for(lambda: app.screen.maximized is query.view, pilot)
                query.editor.load_text("SELECT 42 AS delayed")
                await pilot.press("ctrl+enter")
                await wait_for(started.is_set, pilot)
            gc.collect()
            assert browser.root is None
            assert value() is None
            assert data() is None
            assert not query.is_attached
        finally:
            release.set()
        await asyncio.get_running_loop().shutdown_default_executor()
        assert browser.root is None
        assert not query.is_attached
        notify.assert_not_called()


@pytest.mark.parametrize("query_result", [False, True], ids=["file", "query"])
async def test_tab_switch_restores_container_scroll_independently_of_selection(
    nested_parquet: Path, query_result: bool
) -> None:
    from textual.widgets import TabbedContent

    from tests.helpers import open_query, select_tab

    app = Parqx([nested_parquet])
    async with app.run_test(size=(140, 26)) as pilot:
        table = await wait_for_source_table(app, pilot)
        if query_result:
            table = await run_query(app, pilot, "SELECT range(100) AS value")
        else:
            table.cursor_type = "row"
        assert isinstance(table.parent, TableView)
        browser = table.parent.browser
        table.focus()
        await pilot.press("enter", "tab")
        browser.options.scroll_to(
            y=min(50, browser.options.max_scroll_y), animate=False, force=True
        )
        await pilot.pause()
        position = browser.options.scroll_y
        assert position > 1
        assert browser.options.highlighted == 1
        tabs = app.query_one(TabbedContent)
        pane_id = tabs.active
        await open_query(app, pilot)
        await select_tab(tabs, pane_id, pilot)
        assert browser.options.scroll_y == position
        assert browser.options.highlighted == 1
        table.focus()
        await pilot.press("tab")
        assert browser.options.has_focus
        assert browser.options.scroll_y == position


@pytest.mark.parametrize("kind", ["string", "list"])
@pytest.mark.parametrize("interaction", ["unchanged", "scroll", "new-root"])
async def test_restore_retains_clamped_position_unless_user_changes_inspection(
    small_parquet: Path, kind: str, interaction: str
) -> None:
    app = Parqx([small_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        sql = (
            "SELECT repeat('line\n', 100) AS value"
            if kind == "string"
            else "SELECT range(100) AS value"
        )
        table = await run_query(app, pilot, sql)
        assert isinstance(table.parent, TableView)
        view = table.parent
        browser = view.browser
        await pilot.press("enter", "tab")
        body = browser.reader if kind == "string" else browser.options
        body.scroll_to(y=body.max_scroll_y - 2, animate=False, force=True)
        await pilot.pause()
        position = body.scroll_y
        assert position > 0
        app.screen.action_maximize()
        await pilot.pause()
        assert app.screen.maximized is view
        assert body.scroll_y < position
        expected = position
        if interaction == "scroll":
            body.scroll_to(y=body.scroll_y - 10, animate=False, force=True)
            await pilot.pause()
            expected = body.scroll_y
            assert expected < body.max_scroll_y
        elif interaction == "new-root":
            await pilot.press("escape")
            assert app.screen.maximized is view
            assert table.has_focus
            await pilot.press("enter", "tab")
            expected = 0
        app.screen.action_minimize()
        await pilot.pause()
        assert app.screen.maximized is None
        assert body.has_focus
        assert body.scroll_y == expected
        assert len(browser.query(".value-crumb")) == 1


async def test_maximize_restores_binary_position_after_width_reflow(
    small_parquet: Path,
) -> None:
    app = Parqx([small_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await run_query(
            app, pilot, "SELECT CAST(repeat('binary', 200) AS BLOB) AS payload"
        )
        app.action_toggle_sources()
        assert isinstance(table.parent, TableView)
        browser = table.parent.browser
        await pilot.press("enter", "tab")
        reader = browser.reader
        original_columns = int(reader.text.splitlines()[1].split()[0], 16)
        reader.scroll_to(y=reader.max_scroll_y - 2, animate=False, force=True)
        await pilot.pause()
        position = reader.scroll_y
        app.screen.action_maximize()
        await pilot.pause()
        enlarged_columns = int(reader.text.splitlines()[1].split()[0], 16)
        assert enlarged_columns > original_columns
        app.screen.action_minimize()
        await pilot.pause()
        assert reader.has_focus
        assert int(reader.text.splitlines()[1].split()[0], 16) == original_columns
        assert reader.scroll_y == position
