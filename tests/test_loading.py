from pathlib import Path
from typing import Literal
from unittest.mock import patch

import pytest
from textual.coordinate import Coordinate
from textual.widgets import TextArea

from parqx.data.parquet import ParquetSource
from parqx.query.engine import QuerySession
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from parqx.tui.widgets.query_panel import QueryPanel
from tests.helpers import WorkerGate, wait_for


async def test_initial_browse_keeps_table_mounted_and_editor_usable(
    small_parquet: Path,
) -> None:
    gate = WorkerGate()

    def slow_metadata(path: Path) -> ParquetSource:
        gate.pause()
        return ParquetSource(path)

    app = ParqxApp(small_parquet)
    with patch("parqx.tui.app.ParquetSource", slow_metadata):
        async with app.run_test(size=(100, 32)) as pilot:
            with gate:
                await wait_for(gate.started.is_set, pilot)
                table = app.query_one(ArrowTable)
                assert table.loading
                app.action_toggle_query()
                await pilot.pause()
                panel = app.query_one(QueryPanel)
                panel_region = panel.region
                editor = app.query_one(TextArea)
                editor.load_text("SELECT 42")
                await pilot.press("end", "space")
                assert editor.text == "SELECT 42 "

                gate.release.set()
                await wait_for(lambda: not table.loading, pilot)
                await wait_for(lambda: table.data.peek(0, 0) is not None, pilot)
                assert app.query_one(ArrowTable) is table
                assert table.row_count == 5
                assert panel.region == panel_region
                assert table.region.bottom == panel.region.y


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_initial_sql_clears_loading_and_can_retry(
    small_parquet: Path, outcome: Literal["success", "error", "cancel"]
) -> None:
    gate = WorkerGate()
    original_enter = QuerySession.__enter__

    def slow_enter(session: QuerySession) -> QuerySession:
        gate.pause()
        return original_enter(session)

    sql = "SELECT missing FROM data" if outcome == "error" else "SELECT 42 AS answer"
    app = ParqxApp(small_parquet, initial_sql=sql)
    with patch.object(QuerySession, "__enter__", slow_enter):
        async with app.run_test() as pilot:
            with gate:
                await wait_for(gate.started.is_set, pilot)
                table = app.query_one(ArrowTable)
                control = app._query_control  # pyright: ignore[reportPrivateUsage]
                assert control is not None
                assert table.loading
                assert app.query_one(QueryPanel).display
                if outcome == "cancel":
                    await pilot.press("f2")
                    assert not table.loading
                    assert not app.query_running
                gate.release.set()
                await wait_for(control.finished.is_set, pilot)
                assert not table.loading
                assert not app.query_running
                assert app.query_one(ArrowTable) is table
                if outcome == "success":
                    assert table.get_cell_at(Coordinate(0, 0)).as_py() == 42
                    assert app.query_error is None
                    return
                assert table.column_count == 0
                assert (app.query_error is not None) == (outcome == "error")

                # A failed or cancelled first request must not suppress the
                # loading indicator on a retry before any result exists.
                gate.started.clear()
                gate.release.clear()
                app.query_one(TextArea).load_text("SELECT 7 AS answer")
                await pilot.press("f1")
                await wait_for(gate.started.is_set, pilot)
                assert table.loading
                gate.release.set()
                await wait_for(lambda: not app.query_running, pilot)
                assert not table.loading
                assert app.query_error is None
                assert app.query_one(ArrowTable) is table
                assert table.get_cell_at(Coordinate(0, 0)).as_py() == 7


@pytest.mark.parametrize("metadata_error", [False, True])
async def test_superseded_metadata_cannot_clear_query_loading(
    small_parquet: Path, metadata_error: bool
) -> None:
    metadata_gate = WorkerGate()
    query_gate = WorkerGate()
    original_enter = QuerySession.__enter__

    def slow_metadata(path: Path) -> ParquetSource:
        metadata_gate.pause()
        if metadata_error:
            raise OSError("superseded metadata read failed")
        return ParquetSource(path)

    def slow_enter(session: QuerySession) -> QuerySession:
        query_gate.pause()
        return original_enter(session)

    app = ParqxApp(small_parquet)
    with (
        patch("parqx.tui.app.ParquetSource", slow_metadata),
        patch.object(QuerySession, "__enter__", slow_enter),
    ):
        async with app.run_test() as pilot:
            with metadata_gate, query_gate:
                await wait_for(metadata_gate.started.is_set, pilot)
                table = app.query_one(ArrowTable)
                metadata_worker = next(w for w in app.workers if w.group == "load")
                app.query_one(TextArea).load_text("SELECT 42 AS answer")
                await pilot.press("f1")
                await wait_for(query_gate.started.is_set, pilot)
                metadata_gate.release.set()
                await wait_for(lambda: metadata_worker.is_finished, pilot)
                assert table.loading
                assert table.column_count == 0
                assert app.query_running
                assert app.load_error is None

                query_gate.release.set()
                await wait_for(lambda: not app.query_running, pilot)
                assert not table.loading
                assert table.get_cell_at(Coordinate(0, 0)).as_py() == 42


@pytest.mark.parametrize(
    "initial_sql", ["SELECT 42 AS answer", "SELECT 42 WHERE false"]
)
async def test_new_query_keeps_existing_result_visible(
    small_parquet: Path, initial_sql: str
) -> None:
    gate = WorkerGate()
    original_enter = QuerySession.__enter__

    def slow_enter(session: QuerySession) -> QuerySession:
        gate.pause()
        return original_enter(session)

    app = ParqxApp(small_parquet, initial_sql=initial_sql)
    async with app.run_test() as pilot:
        await wait_for(lambda: not app.query_running, pilot)
        table = app.query_one(ArrowTable)
        previous_data = table.data
        with patch.object(QuerySession, "__enter__", slow_enter), gate:
            app.query_one(TextArea).load_text("SELECT 7 AS answer")
            await pilot.press("f1")
            await wait_for(gate.started.is_set, pilot)
            assert not table.loading
            assert table.data is previous_data
            gate.release.set()
            await wait_for(lambda: not app.query_running, pilot)
            assert not table.loading
            assert app.query_one(ArrowTable) is table
            assert table.get_cell_at(Coordinate(0, 0)).as_py() == 7
