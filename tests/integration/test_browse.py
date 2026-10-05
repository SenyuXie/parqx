"""Viewport rendering and lazy source reads across the widget/app boundary."""

import asyncio
import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident
from unittest.mock import patch

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from textual.app import App, ComposeResult
from textual.coordinate import Coordinate

from parqx.catalog import SourceCatalog
from parqx.data.duckdb import QueryControl
from parqx.data.parquet import ParquetSource
from parqx.data.view import DataPage, TableData
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable, SourcePane
from tests.helpers import open_query, wait_for


async def test_replace_table_resets_mounted_layout() -> None:
    table = ArrowTable(
        pa.table({f"old_{index}": list(range(200)) for index in range(30)}),
        cursor_type="cell",
        zebra_stripes=True,
        cell_padding=2,
    )

    class TableApp(App[None]):
        def compose(self) -> ComposeResult:
            yield table

    async with TableApp().run_test(size=(60, 12)) as pilot:
        table.move_cursor(row=150, column=25, animate=False)
        await pilot.pause()
        table.scroll_to(x=100, y=100, animate=False, force=True)
        await pilot.pause()
        await pilot.hover(table, offset=(12, 3))
        assert table.scroll_x > 0
        assert table.scroll_y > 0
        assert table.hover_coordinate != Coordinate(0, 0)
        old_size = table.virtual_size

        table.replace_table(pa.table({"new": ["replacement", "second"]}))
        await pilot.pause()

        assert (table.row_count, table.column_count) == (2, 1)
        assert table.columns[0].name == "new"
        assert table.columns[0].content_width == len("replacement")
        assert table.index_column.content_width == 1
        assert table.virtual_size.height == 3  # Two data rows and the header.
        assert table.virtual_size.width < old_size.width
        assert (table.scroll_x, table.scroll_y) == (0, 0)
        assert table.cursor_coordinate == Coordinate(0, 0)
        assert table.hover_coordinate == Coordinate(0, 0)
        assert table.cursor_type == "cell"
        assert table.zebra_stripes is True
        assert table.cell_padding == 2
        assert "new" in table.render_line(0).text
        assert "replacement" in table.render_line(1).text

        # Empty results still expose their schema and can be replaced again.
        table.replace_table(pa.table({"empty": pa.array([], type=pa.int64())}))
        await pilot.pause()
        assert table.row_count == 0
        assert table.virtual_size.height == 1
        assert "empty" in table.render_line(0).text

        table.replace_table(pa.table({"restored": [42]}))
        await pilot.pause()
        assert table.virtual_size.height == 2
        assert "restored" in table.render_line(0).text
        assert "42" in table.render_line(1).text


async def test_window_requests_are_coalesced_and_partial_pages_fill_the_view() -> None:
    schema = pa.table({"value": [0]}).schema
    data = TableData(schema, 10_000)
    table = ArrowTable(data)
    requests: list[ArrowTable.WindowRequested] = []
    selected: list[ArrowTable.CellSelected] = []

    class TableApp(App[None]):
        def compose(self) -> ComposeResult:
            yield table

        def on_arrow_table_window_requested(
            self, event: ArrowTable.WindowRequested
        ) -> None:
            requests.append(event)

        def on_arrow_table_cell_selected(self, event: ArrowTable.CellSelected) -> None:
            selected.append(event)

    async with TableApp().run_test(size=(60, 12)) as pilot:
        await pilot.pause()
        assert len(requests) == 1
        assert requests[0].control is table
        assert requests[0].data is data
        assert (requests[0].start_row, requests[0].stop_row) == (0, 256)
        await pilot.press("enter")
        assert not selected
        assert len(requests) == 1

        # A byte-limited read may supply fewer rows than the requested window.
        table.accept_page(DataPage(0, pa.table({"value": [0, 1]})))
        await pilot.pause()
        assert len(requests) == 2
        assert requests[-1].start_row == 2
        table.accept_page(DataPage(2, pa.table({"value": range(2, 258)})))
        await pilot.pause()
        assert len(requests) == 2
        await pilot.press("enter")
        assert selected[-1].value.as_py() == 0

        table.move_cursor(row=9_000, animate=False)
        await pilot.pause()
        request = requests[-1]
        assert request.start_row > 8_900
        assert request.stop_row - request.start_row <= 256
        table.accept_page(
            DataPage(
                request.start_row,
                pa.table({"value": range(request.start_row, request.stop_row)}),
            )
        )
        await pilot.pause()
        assert table.cursor_coordinate == Coordinate(9_000, 0)
        assert table.get_cell_at(table.cursor_coordinate).as_py() == 9_000


async def test_small_cache_does_not_reload_visible_rows_forever() -> None:
    data = TableData(pa.table({"value": [0]}).schema, 1_000, cache_bytes=8)
    table = ArrowTable(data)
    requests: list[int] = []

    class TableApp(App[None]):
        def compose(self) -> ComposeResult:
            yield table

        def on_arrow_table_window_requested(
            self, event: ArrowTable.WindowRequested
        ) -> None:
            requests.append(event.start_row)
            assert len(requests) < 50, "Evicted rows must not cause a repaint/read loop"
            table.accept_page(
                DataPage(event.start_row, pa.table({"value": [event.start_row]}))
            )

    async with TableApp().run_test(size=(60, 12)) as pilot:

        async def wait_for_visible_rows() -> None:
            async with asyncio.timeout(5):
                while True:
                    await pilot.pause()
                    top = round(table.scroll_y)
                    if top <= table.cursor_row < top + table.size.height and all(
                        "…" not in table.render_line(row).text
                        for row in range(table.size.height)
                    ):
                        return

        await wait_for_visible_rows()
        assert 1 < len(requests) <= table.size.height
        count = len(requests)
        assert data.cache_bytes <= 8
        table.refresh()
        await pilot.pause()
        assert len(requests) == count

        table.move_cursor(row=950, animate=False)
        await wait_for_visible_rows()
        assert count < len(requests) <= count + table.size.height + 1
        assert data.cache_bytes <= 8
        count = len(requests)
        table.refresh()
        await pilot.pause()
        assert len(requests) == count


async def test_source_reads_bounded_windows_off_ui_thread(tmp_path: Path) -> None:
    path = tmp_path / "large.parquet"
    pq.write_table(pa.table({"n": range(100_000)}), path, row_group_size=10_000)
    ui_thread = get_ident()
    reads: list[tuple[int, int]] = []
    original_read = ParquetSource.read_window

    def record_read(
        source: ParquetSource, start: int, stop: int, control: QueryControl
    ) -> DataPage:
        assert get_ident() != ui_thread
        reads.append((start, stop))
        return original_read(source, start, stop, control)

    app = ParqxApp([path])
    with (
        patch(
            "pyarrow.parquet.ParquetFile", side_effect=AssertionError("PyArrow read")
        ),
        patch.object(ParquetSource, "read_window", record_read),
    ):
        async with app.run_test(size=(60, 15)) as pilot:
            await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
            table = app.query_one(ArrowTable)
            assert table.row_count == 100_000
            await wait_for(lambda: table.data.peek(0, 0) is not None, pilot)
            assert table.data.peek(50_000, 0) is None
            await pilot.press("ctrl+end")
            await wait_for(lambda: table.data.peek(99_999, 0) is not None, pilot)
            assert table.get_cell_at(Coordinate(99_999, 0)).as_py() == 99_999
            assert table.cursor_row == 99_999
            assert len(reads) <= 4
            assert all(0 < stop - start <= 4_096 for start, stop in reads)
            assert table.data.cache_bytes <= table.data.cache_budget

            table.focus()
            await pilot.press("ctrl+home")
            await wait_for(lambda: table.data.peek(0, 0) is not None, pilot)
            assert table.get_cell_at(Coordinate(0, 0)).as_py() == 0


async def test_read_ahead_serves_consecutive_windows_from_cache(tmp_path: Path) -> None:
    path = tmp_path / "read-ahead.parquet"
    pq.write_table(pa.table({"n": range(10_000)}), path, row_group_size=1_000)
    reads: list[tuple[int, int]] = []
    original_read = ParquetSource.read_window

    def record_read(
        source: ParquetSource, start: int, stop: int, control: QueryControl
    ) -> DataPage:
        reads.append((start, stop))
        return original_read(source, start, stop, control)

    app = ParqxApp([path])
    with patch.object(ParquetSource, "read_window", record_read):
        async with app.run_test(size=(60, 15)) as pilot:
            await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
            table = app.query_one(ArrowTable)
            await wait_for(lambda: table.data.peek(4_095, 0) is not None, pilot)
            assert reads == [(0, 4_096)]
            assert table.data.peek(4_096, 0) is None

            for row in (256, 512, 1_024, 2_048, 4_000):
                table.move_cursor(row=row, animate=False)
                await pilot.pause()
                assert table.cursor_row == row
                assert table.get_cell_at(Coordinate(row, 0)).as_py() == row
                assert reads == [(0, 4_096)]

            table.move_cursor(row=4_096, animate=False)
            await wait_for(lambda: table.data.peek(4_096, 0) is not None, pilot)
            assert reads == [(0, 4_096), (4_096, 8_192)]
            assert table.get_cell_at(Coordinate(4_096, 0)).as_py() == 4_096
            assert table.data.cache_bytes <= table.data.cache_budget


async def test_pending_source_window_keeps_modal_focus(small_parquet: Path) -> None:
    started, release = Event(), Event()
    original_read = ParquetSource.read_window

    def delayed_read(
        source: ParquetSource, start: int, stop: int, control: QueryControl
    ) -> DataPage:
        started.set()
        release.wait(timeout=5)
        return original_read(source, start, stop, control)

    app = ParqxApp([small_parquet])
    with patch.object(ParquetSource, "read_window", delayed_read):
        async with app.run_test() as pilot:
            try:
                await wait_for(started.is_set, pilot)
                table = app.query_one(ArrowTable)
                assert table.data.peek(0, 0) is None
                await pilot.press("down", "right", "enter")
                assert table.cursor_coordinate == Coordinate(1, 1)
                query = await open_query(app, pilot)
                screen = app.screen
                release.set()
                await wait_for(lambda: table.data.peek(0, 0) is not None, pilot)
                assert app.screen is screen
                assert query.editor.has_focus
                assert table.cursor_coordinate == Coordinate(1, 1)
            finally:
                release.set()


async def test_new_window_cancels_old_read_and_discards_late_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "superseded.parquet"
    pq.write_table(pa.table({"n": range(1_000)}), path, row_group_size=100)
    started, release, returned, interrupted = Event(), Event(), Event(), Event()
    cancellation: list[Event] = []
    original_read = ParquetSource.read_window
    original_interrupt = duckdb.DuckDBPyConnection.interrupt

    def record_interrupt(connection: duckdb.DuckDBPyConnection) -> None:
        original_interrupt(connection)
        interrupted.set()

    def delayed_first_read(
        source: ParquetSource, start: int, stop: int, control: QueryControl
    ) -> DataPage:
        if not started.is_set():
            cancellation.append(control.cancelled)
            with duckdb.connect() as connection:
                control.attach(connection)
                started.set()
                try:
                    assert release.wait(timeout=15)
                    # Native work can finish after its worker was cancelled.
                    raise OSError("obsolete window failed")
                finally:
                    control.detach()
                    returned.set()
        return original_read(source, start, stop, control)

    app = ParqxApp([path])
    with (
        patch.object(ParquetSource, "read_window", delayed_first_read),
        patch.object(duckdb.DuckDBPyConnection, "interrupt", record_interrupt),
        patch.object(app, "notify", wraps=app.notify) as notify,
    ):
        async with app.run_test(size=(80, 18)) as pilot:
            try:
                await wait_for(started.is_set, pilot)
                table = app.query_one(ArrowTable)
                await pilot.press("ctrl+end")
                await wait_for(lambda: table.data.peek(999, 0) is not None, pilot)
                assert cancellation[0].is_set()
                assert interrupted.is_set()
                assert not returned.is_set()
                assert table.get_cell_at(Coordinate(999, 0)).as_py() == 999

                release.set()
                await wait_for(returned.is_set, pilot)
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                await pilot.pause()
                assert table.data.peek(0, 0) is None
                assert table.get_cell_at(Coordinate(999, 0)).as_py() == 999
                notify.assert_not_called()
            finally:
                release.set()


async def test_unstarted_page_workers_do_not_leave_unawaited_coroutines(
    tmp_path: Path, recwarn: pytest.WarningsRecorder
) -> None:
    path = tmp_path / "rapid.parquet"
    pq.write_table(pa.table({"n": range(10_000)}), path, row_group_size=100)
    reads: list[int] = []
    original_read = ParquetSource.read_window

    def record_read(
        source: ParquetSource, start: int, stop: int, control: QueryControl
    ) -> DataPage:
        reads.append(start)
        return original_read(source, start, stop, control)

    app = ParqxApp([path])
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        table = app.query_one(ArrowTable)
        await wait_for(lambda: table.data.peek(0, 0) is not None, pilot)
        with patch.object(ParquetSource, "read_window", record_read):
            # Deliver multiple requests before any new worker can start.
            pane = app.query_one(SourcePane)
            for start in (5_000, 7_000, 9_000):
                pane._on_window_requested(  # pyright: ignore[reportPrivateUsage]
                    ArrowTable.WindowRequested(table, table.data, start, start + 100)
                )
            await wait_for(lambda: table.data.peek(9_999, 0) is not None, pilot)
            assert reads == [9_000]
            assert table.get_cell_at(Coordinate(9_999, 0)).as_py() == 9_999
            await pilot.pause()
    gc.collect()
    assert not any(
        issubclass(warning.category, RuntimeWarning)
        and "was never awaited" in str(warning.message)
        for warning in recwarn
    )


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
async def test_window_error_waits_for_navigation_before_retry(
    tmp_path: Path, error_type: type[Exception], caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "retry.parquet"
    pq.write_table(pa.table({"n": range(1_000)}), path, row_group_size=100)
    original_read = ParquetSource.read_window
    calls = 0

    def fail_first_read(
        source: ParquetSource, start: int, stop: int, control: QueryControl
    ) -> DataPage:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error_type("window temporarily unavailable")
        return original_read(source, start, stop, control)

    app = ParqxApp([path])
    with (
        patch.object(ParquetSource, "read_window", fail_first_read),
        patch.object(app, "notify", wraps=app.notify) as notify,
    ):
        async with app.run_test() as pilot:
            await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
            table = app.query_one(ArrowTable)
            await wait_for(lambda: notify.called, pilot)
            message = str(notify.call_args.args[0])
            assert "temporarily unavailable" in message
            assert str(path.resolve()) in message
            assert '"retry"' in message
            assert notify.call_args.kwargs["severity"] == "error"
            assert notify.call_args.kwargs["markup"] is False
            record = next(
                record
                for record in caplog.records
                if record.name == "parqx.tui.widgets.source_pane"
            )
            assert record.exc_info is not None
            assert record.exc_info[0] is error_type
            assert record.exc_info[2] is not None
            table.refresh()
            await pilot.pause()
            assert calls == 1
            assert not app.load_errors
            await pilot.press("ctrl+end")
            await wait_for(lambda: table.data.peek(999, 0) is not None, pilot)
            assert table.get_cell_at(Coordinate(999, 0)).as_py() == 999
            assert notify.call_count == 1


async def test_shutdown_cancels_pending_source_window(small_parquet: Path) -> None:
    started, finished = Event(), Event()
    cancellation: list[Event] = []
    original_read = ParquetSource.read_window

    def wait_for_cancel(
        source: ParquetSource, start: int, stop: int, control: QueryControl
    ) -> DataPage:
        cancellation.append(control.cancelled)
        started.set()
        control.cancelled.wait(timeout=5)
        try:
            return original_read(source, start, stop, control)
        finally:
            finished.set()

    app = ParqxApp([small_parquet])
    with patch.object(ParquetSource, "read_window", wait_for_cancel):
        async with app.run_test() as pilot:
            await wait_for(started.is_set, pilot)
    assert all(event.is_set() for event in cancellation)
    assert finished.wait(timeout=2)


async def test_source_pane_removal_cancels_read_and_releases_cache(
    small_parquet: Path,
) -> None:
    started, release, returned = Event(), Event(), Event()
    controls: list[QueryControl] = []
    source = ParquetSource(small_parquet)
    spec = SourceCatalog([small_parquet]).entries[0].spec

    def delayed_read(
        source: ParquetSource, start: int, stop: int, control: QueryControl
    ) -> DataPage:
        controls.append(control)
        started.set()
        try:
            assert release.wait(timeout=10)
            return DataPage(start, pa.table({"late": [99]}))
        finally:
            returned.set()

    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        patch.object(ParquetSource, "read_window", delayed_read),
    ):
        pane = SourcePane(spec.display_name, spec, executor)

        class PaneApp(App[None]):
            def compose(self) -> ComposeResult:
                yield pane

        app = PaneApp()
        try:
            async with app.run_test() as pilot:
                await pane.set_source(source)
                await wait_for(started.is_set, pilot)
                assert pane.table is not None
                cache = weakref.ref(pane.table.data)
                page_worker = next(
                    worker for worker in app.workers if worker.group == "page"
                )
                assert page_worker.node is pane

                await pane.remove()
                await wait_for(controls[0].cancelled.is_set, pilot)
                assert pane.table is None
                assert not returned.is_set()

                def cache_released() -> bool:
                    gc.collect()
                    return cache() is None

                await wait_for(cache_released, pilot)
                release.set()
                await wait_for(returned.is_set, pilot)
        finally:
            release.set()


@pytest.mark.parametrize("fail", [False, True])
async def test_replacing_source_data_discards_pending_page_and_error(
    small_parquet: Path, fail: bool
) -> None:
    started, release = Event(), Event()

    def delayed_read(
        source: ParquetSource, start: int, stop: int, control: QueryControl
    ) -> DataPage:
        started.set()
        assert release.wait(timeout=10)
        if fail:
            raise OSError("obsolete data failed")
        return DataPage(start, pa.table({"obsolete": [99]}))

    app = ParqxApp([small_parquet])
    with (
        patch.object(ParquetSource, "read_window", delayed_read),
        patch.object(app, "notify", wraps=app.notify) as notify,
    ):
        try:
            async with app.run_test() as pilot:
                await wait_for(started.is_set, pilot)
                table = app.query_one(ArrowTable)
                old_data = weakref.ref(table.data)
                table.replace_table(pa.table({"current": [42]}))
                with (
                    patch.object(
                        table, "accept_page", wraps=table.accept_page
                    ) as accept,
                    patch.object(
                        table, "fail_window", wraps=table.fail_window
                    ) as error,
                ):
                    release.set()
                    await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                    await pilot.pause()
                    assert table.get_cell_at(Coordinate(0, 0)).as_py() == 42
                    assert table.data.cache_bytes == 0
                    gc.collect()
                    assert old_data() is None
                    accept.assert_not_called()
                    error.assert_not_called()
                    notify.assert_not_called()
        finally:
            release.set()
