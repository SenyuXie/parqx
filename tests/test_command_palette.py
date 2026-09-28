from pathlib import Path
from unittest.mock import patch

import pytest
from textual.command import CommandPalette
from textual.coordinate import Coordinate
from textual.widgets import Button, Footer, Input, Label, TextArea

from parqx.query.engine import QueryLimits, QuerySession
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from parqx.tui.widgets.query_panel import QueryPanel
from tests.helpers import WorkerGate, toggle_sql_query, wait_for


async def test_palette_toggles_sql_query_and_runs_via_shortcut(
    small_parquet: Path,
) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test(size=(100, 32)) as pilot:
        table = app.query_one(ArrowTable)
        await wait_for(lambda: not table.loading, pilot)
        panel = app.query_one(QueryPanel)
        editor = app.query_one(TextArea)
        assert not panel.query(Button)
        assert editor.id == "sql-query"
        assert editor.border_title == "SQL query"
        assert editor.border_subtitle == (
            "F1 Run · F2 Cancel · F3 Browse · F4 All · Esc Back"
        )
        original_data = table.data
        assert not panel.display
        assert {"Theme", "Quit", "Keys", "SQL query"} <= {
            command.title for command in app.get_system_commands(app.screen)
        }
        await pilot.press("f2", "f4", "f5", "ctrl+enter")
        assert not panel.display
        assert table.data is original_data

        await toggle_sql_query(pilot, "Show the SQL query panel")
        assert panel.display
        assert editor.has_focus
        editor.load_text("SELECT 42 AS answer")
        await pilot.press("f2", "f4", "f5", "ctrl+enter")
        assert panel.display
        assert table.data is original_data
        await toggle_sql_query(pilot, "Hide the SQL query panel")
        assert not panel.display
        assert table.has_focus
        assert table.data is original_data
        await toggle_sql_query(pilot, "Show the SQL query panel")
        assert editor.text == "SELECT 42 AS answer"
        assert editor.has_focus
        assert table.data is original_data
        await pilot.press("f1")
        await wait_for(lambda: not app.query_running, pilot)
        assert app.query_error is None
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == 42

        result_data = table.data
        await toggle_sql_query(pilot, "Hide the SQL query panel")
        assert not panel.display
        assert table.has_focus
        assert table.data is result_data
        await toggle_sql_query(pilot, "Show the SQL query panel")
        assert editor.has_focus
        assert editor.text == "SELECT 42 AS answer"
        assert table.data is result_data

        editor.load_text("SELECT 7 AS answer")
        await pilot.press("f1")
        await wait_for(lambda: not app.query_running, pilot)
        assert app.query_error is None
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == 7


@pytest.mark.parametrize("query_error", [False, True])
async def test_query_can_finish_while_palette_is_open(
    small_parquet: Path, query_error: bool
) -> None:
    gate = WorkerGate()
    original_enter = QuerySession.__enter__

    def slow_enter(session: QuerySession) -> QuerySession:
        gate.pause()
        return original_enter(session)

    sql = "SELECT missing FROM data" if query_error else "SELECT 42 AS answer"
    app = ParqxApp(small_parquet, initial_sql=sql)
    with patch.object(QuerySession, "__enter__", slow_enter):
        async with app.run_test() as pilot:
            with gate:
                await wait_for(gate.started.is_set, pilot)
                await pilot.press("ctrl+p")
                assert isinstance(app.screen, CommandPalette)
                control = app._query_control  # pyright: ignore[reportPrivateUsage]
                assert control is not None
                await pilot.press("f1", "f2", "f3", "f4")
                assert app._query_control is control  # pyright: ignore[reportPrivateUsage]
                assert not control.cancelled.is_set()
                assert not control.load_all.is_set()
                await pilot.press("escape")
                assert not isinstance(app.screen, CommandPalette)
                assert app.query_running

                await pilot.press("ctrl+p")
                palette = app.screen
                assert isinstance(palette, CommandPalette)
                gate.release.set()
                await wait_for(lambda: not app.query_running, pilot)
                assert app.screen is palette
                assert palette.query_one(Input).has_focus
                assert (app.query_error is not None) == query_error
                await pilot.press("escape")
                assert app.screen is not palette
                assert not app.query_one(ArrowTable).loading


async def test_palette_keys_do_not_run_cancel_browse_or_resume_a_query(
    small_parquet: Path,
) -> None:
    app = ParqxApp(
        small_parquet,
        initial_sql="SELECT * FROM data",
        query_limits=QueryLimits(preview_rows=1),
    )
    async with app.run_test() as pilot:
        await wait_for(lambda: app.can_load_all, pilot)
        control = app._query_control  # pyright: ignore[reportPrivateUsage]
        assert control is not None
        data = app.query_one(ArrowTable).data
        await pilot.press("ctrl+p", "f1", "f2", "f3", "f4")
        assert isinstance(app.screen, CommandPalette)
        for action in ("run_query", "cancel_query", "browse", "load_all"):
            assert app.check_action(action, ()) is False
        assert app._query_control is control  # pyright: ignore[reportPrivateUsage]
        assert app.query_one(ArrowTable).data is data
        assert not control.load_all.is_set()
        assert not app.query_running
        await pilot.press("escape")
        assert not isinstance(app.screen, CommandPalette)
        assert not control.cancelled.is_set()
        assert app.can_load_all
        await pilot.press("escape")
        assert not control.cancelled.is_set()
        await pilot.press("f2")
        assert control.cancelled.is_set()


async def test_bottom_dock_resizes_without_overlapping_table_or_footer(
    small_parquet: Path,
) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test(size=(100, 32)) as pilot:
        table = app.query_one(ArrowTable)
        await wait_for(lambda: not table.loading, pilot)
        panel = app.query_one(QueryPanel)
        status = app.query_one("#query-status", Label)
        footer = app.query_one(Footer)
        dock = app.query_one("#bottom-area")
        for width, height in [(100, 32), (60, 18), (140, 50)]:
            await pilot.resize_terminal(width, height)
            app.action_toggle_query()
            status.update("line one\nline two\nline three")
            await pilot.pause()
            assert panel.region.height == 7
            assert panel.editor.region.height == 7
            assert table.region.bottom == dock.region.y == panel.region.y
            assert panel.region.bottom == status.region.y
            assert status.region.bottom == footer.region.y
            assert footer.region.bottom == height
            assert dock.region.width == width
            shown_height = table.region.height
            app.action_toggle_query()
            await pilot.pause()
            assert table.region.bottom == dock.region.y == status.region.y
            assert table.region.height == shown_height + 7
            assert status.region.bottom == footer.region.y
            assert footer.region.bottom == height
