from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pytest
from textual.coordinate import Coordinate
from textual.widgets import TextArea

from parqx.data.result_store import ResultStore
from parqx.query.engine import QueryLimits
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from tests.helpers import WorkerGate, wait_for


async def test_initial_sql_bypasses_full_source_read(small_parquet: Path) -> None:
    app = ParqxApp(small_parquet, initial_sql="SELECT name FROM data WHERE id = 3")
    with patch("pyarrow.parquet.read_table", side_effect=AssertionError("full read")):
        async with app.run_test(size=(100, 32)) as pilot:
            await wait_for(lambda: not app.query_one(ArrowTable).loading, pilot)
            table = app.query_one(ArrowTable)
            assert table.row_count == 1
            assert table.columns[0].name == "name"
            assert table.size.height >= 10
            assert app.query_error is None


@pytest.mark.parametrize("focus_sql", [False, True])
async def test_query_replace_error_empty_and_browse(
    small_parquet: Path, focus_sql: bool
) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test(size=(100, 32)) as pilot:
        await wait_for(lambda: not app.query_one(ArrowTable).loading, pilot)
        app.action_toggle_query()
        await pilot.pause()
        editor = app.query_one(TextArea)
        table = app.query_one(ArrowTable)
        target = editor if focus_sql else table
        target.focus()
        editor.load_text("SELECT id FROM data WHERE id > 3")
        await pilot.press("f1")
        await wait_for(lambda: not app.query_running, pilot)
        assert (table.row_count, table.column_count) == (2, 1)
        assert table.has_focus
        editor.load_text("SELECT missing FROM data")
        target.focus()
        await pilot.press("f1")
        await wait_for(lambda: app.query_error is not None, pilot)
        assert table.row_count == 2
        assert editor.has_focus
        editor.load_text("SELECT name FROM data WHERE false")
        target.focus()
        await pilot.press("f1")
        await wait_for(lambda: not app.query_running, pilot)
        assert table.row_count == 0
        assert table.columns[0].name == "name"
        target.focus()
        await pilot.press("f3")
        await wait_for(lambda: table.row_count == 5, pilot)
        assert table.column_count == 3
        assert table.has_focus


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_preview_and_new_query_interrupt_prior_work(
    small_parquet: Path, cancel_first: bool
) -> None:
    app = ParqxApp(
        small_parquet,
        initial_sql="SELECT * FROM data",
        query_limits=QueryLimits(preview_rows=2),
    )
    async with app.run_test() as pilot:
        await wait_for(lambda: not app.query_one(ArrowTable).loading, pilot)
        assert app.query_one(ArrowTable).row_count == 2
        assert app.query_one(ArrowTable).data.total_rows is None
        editor = app.query_one(TextArea)
        editor.load_text("SELECT sum(sin(i)) FROM range(1000000000) t(i)")
        await pilot.press("f1")
        control = app._query_control  # pyright: ignore[reportPrivateUsage]
        assert control is not None
        await wait_for(control.started.is_set, pilot)
        assert not app.query_one(ArrowTable).loading
        if cancel_first:
            await pilot.press("f2")
            assert not app.query_running
        editor.load_text("SELECT 42 AS answer")
        await pilot.press("f1")
        await wait_for(lambda: not app.query_running, pilot)
        await wait_for(control.finished.is_set, pilot)
        assert app.query_error is None
        table = app.query_one(ArrowTable)
        assert table.row_count == 1
        assert table.columns[0].name == "answer"


async def test_editor_text_does_not_trigger_table_bindings(small_parquet: Path) -> None:
    app = ParqxApp(small_parquet)
    async with app.run_test() as pilot:
        await wait_for(lambda: not app.query_one(ArrowTable).loading, pilot)
        app.action_toggle_query()
        await pilot.press("h", "i", "z", "c")
        table = app.query_one(ArrowTable)
        assert table.show_header
        assert table.show_row_index
        assert not table.zebra_stripes
        assert table.cursor_type == "cell"


@pytest.mark.parametrize("focus_sql", [False, True])
async def test_load_all_resumes_once_and_cleans_up(
    small_parquet: Path, focus_sql: bool
) -> None:
    app = ParqxApp(
        small_parquet,
        initial_sql="SELECT i, random() AS value FROM range(20000) t(i)",
        query_limits=QueryLimits(preview_rows=3, batch_rows=256),
    )
    from parqx.query.engine import QuerySession

    executions: list[str] = []
    original = QuerySession.__enter__

    def record_execution(session: QuerySession) -> QuerySession:
        executions.append(session.sql)
        return original(session)

    with patch.object(QuerySession, "__enter__", record_execution):
        async with app.run_test() as pilot:
            await wait_for(lambda: app.can_load_all, pilot)
            widget = app.query_one(ArrowTable)
            if focus_sql:
                app.query_one(TextArea).focus()
            preview_value = widget.get_cell_at(Coordinate(0, 1)).as_py()
            await pilot.press("f4")
            assert not app.can_load_all
            assert not widget.loading
            await wait_for(lambda: not app.query_running, pilot)
            assert widget.row_count == 20_000
            assert widget.data.total_rows == 20_000
            widget.focus()
            await pilot.press("ctrl+end")
            await wait_for(lambda: widget.data.peek(19_999, 0) is not None, pilot)
            assert widget.get_cell_at(Coordinate(19_999, 0)).as_py() == 19_999
            await pilot.press("ctrl+home")
            await wait_for(lambda: widget.data.peek(0, 1) is not None, pilot)
            assert widget.get_cell_at(Coordinate(0, 1)).as_py() == preview_value
            assert len(executions) == 1
            # Capture the store through the read backend to verify shutdown cleanup.
            source = app._window_source  # pyright: ignore[reportPrivateUsage]
            assert isinstance(source, ResultStore)
            directory = source.directory
            assert directory.exists()
    assert not directory.exists()


@pytest.mark.parametrize("focus_sql", [False, True])
async def test_cancel_paused_preview_releases_session(
    small_parquet: Path, focus_sql: bool
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
        if focus_sql:
            editor = app.query_one(TextArea)
            editor.focus()
            await pilot.press("f6", "f7")
            assert editor.selected_text == editor.text
            assert not control.load_all.is_set()
        await pilot.press("escape")
        assert not control.cancelled.is_set()
        assert app.can_load_all
        if focus_sql:
            await pilot.press("tab")
        await pilot.press("f2")
        await wait_for(control.finished.is_set, pilot)
        assert not app.can_load_all
        assert app.query_one(ArrowTable).row_count == 1
        data = app.query_one(ArrowTable).data
        await pilot.press("f4")
        assert app.query_one(ArrowTable).data is data
        assert app._query_control is None  # pyright: ignore[reportPrivateUsage]
        assert not app.query_running


async def test_cancel_full_load_retains_prefix_and_browse_removes_store(
    small_parquet: Path,
) -> None:
    gate = WorkerGate()
    original = ResultStore.append

    def slow_append(store: ResultStore, batch: pa.RecordBatch) -> None:
        if store.row_count >= 3:
            gate.pause()
        original(store, batch)

    app = ParqxApp(
        small_parquet,
        initial_sql="SELECT i FROM range(100000) t(i)",
        query_limits=QueryLimits(preview_rows=3),
    )
    with gate, patch.object(ResultStore, "append", slow_append):
        async with app.run_test() as pilot:
            await wait_for(lambda: app.can_load_all, pilot)
            await pilot.press("f4")
            await wait_for(gate.started.is_set, pilot)
            widget = app.query_one(ArrowTable)
            assert not widget.loading
            assert widget.data.total_rows is None
            source = app._window_source  # pyright: ignore[reportPrivateUsage]
            control = app._query_control  # pyright: ignore[reportPrivateUsage]
            assert isinstance(source, ResultStore)
            assert control is not None
            await pilot.press("f2")
            gate.release.set()
            await wait_for(control.finished.is_set, pilot)
            assert widget.row_count == 3
            assert not app.query_running
            await pilot.press("f3")
            await wait_for(lambda: widget.row_count == 5, pilot)
            await wait_for(lambda: not source.directory.exists(), pilot)


async def test_full_result_write_error_keeps_displayed_prefix(
    small_parquet: Path,
) -> None:
    original = ResultStore.append

    def failing_append(store: ResultStore, batch: pa.RecordBatch) -> None:
        if store.row_count >= 3:
            raise OSError("disk full")
        original(store, batch)

    app = ParqxApp(
        small_parquet,
        initial_sql="SELECT i FROM range(100) t(i)",
        query_limits=QueryLimits(preview_rows=3),
    )
    with patch.object(ResultStore, "append", failing_append):
        async with app.run_test() as pilot:
            await wait_for(lambda: app.can_load_all, pilot)
            await pilot.press("f4")
            await wait_for(lambda: app.query_error is not None, pilot)
            assert app.query_error == "disk full"
            assert not app.query_running
            widget = app.query_one(ArrowTable)
            assert widget.row_count == 3
            assert widget.data.total_rows is None


async def test_shutdown_cleans_superseded_store_even_if_cleanup_was_not_started(
    small_parquet: Path,
) -> None:
    app = ParqxApp(
        small_parquet,
        initial_sql="SELECT i FROM range(100) t(i)",
        query_limits=QueryLimits(preview_rows=3),
    )
    async with app.run_test() as pilot:
        await wait_for(lambda: app.can_load_all, pilot)
        await pilot.press("f4")
        await wait_for(lambda: not app.query_running, pilot)
        source = app._window_source  # pyright: ignore[reportPrivateUsage]
        assert isinstance(source, ResultStore)
        with patch.object(app, "_close_result"):
            await pilot.press("f3")
            await wait_for(lambda: app.query_one(ArrowTable).row_count == 5, pilot)
        assert source.directory.exists()
    assert source.closed
    assert not source.directory.exists()
