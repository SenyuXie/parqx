"""Windowed source browsing with independent SQL tabs and modal input."""

import gc
import weakref
from pathlib import Path
from threading import Event, get_ident
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from textual.command import CommandPalette
from textual.coordinate import Coordinate
from textual.widget import Widget
from textual.widgets import Input, TabbedContent

from parqx.data.parquet import ParquetSource
from parqx.data.view import DataPage
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from tests.test_query_app import open_query, run_query, select_tab, wait_for


@pytest.mark.parametrize(
    "payload", ["hello " * 50_000, b"x" * 300_000], ids=["text", "binary"]
)
async def test_large_field_does_not_hide_neighboring_column(
    tmp_path: Path, payload: str | bytes
) -> None:
    path = tmp_path / "large-field.parquet"
    pq.write_table(pa.table({"id": [123456789], "payload": [payload]}), path)
    app = ParqxApp([path])
    async with app.run_test(size=(80, 12)) as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        table = app.query_one(ArrowTable)
        await wait_for(lambda: table.data.peek(0, 0) is not None, pilot)
        assert table.columns[0].content_width == 9
        assert "123456789" in table.render_line(1).text


async def test_source_reads_bounded_windows_off_ui_thread(tmp_path: Path) -> None:
    path = tmp_path / "large.parquet"
    pq.write_table(pa.table({"n": range(100_000)}), path, row_group_size=10_000)
    ui_thread = get_ident()
    reads: list[tuple[int, int]] = []
    original_read = ParquetSource.read_window

    def record_read(
        source: ParquetSource, start: int, stop: int, cancelled: Event
    ) -> DataPage:
        assert get_ident() != ui_thread
        reads.append((start, stop))
        return original_read(source, start, stop, cancelled)

    app = ParqxApp([path])
    with (
        patch("pyarrow.parquet.read_table", side_effect=AssertionError("full read")),
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
            assert table.data.cache_bytes <= table.data.cache_budget

            await pilot.press("z", "c")
            state = (table.cursor_coordinate, table.scroll_y, table.cursor_type)
            await run_query(app, pilot, "SELECT 42 AS answer")
            tabs = app.query_one(TabbedContent)
            await select_tab(tabs, "source-1", pilot)
            assert (table.cursor_coordinate, table.scroll_y, table.cursor_type) == state
            assert table.zebra_stripes
            table.focus()
            await pilot.press("ctrl+home")
            await wait_for(lambda: table.data.peek(0, 0) is not None, pilot)
            assert table.get_cell_at(Coordinate(0, 0)).as_py() == 0


@pytest.mark.parametrize("overlay", ["query", "palette"])
async def test_pending_source_window_keeps_modal_focus(
    small_parquet: Path, overlay: str
) -> None:
    started, release = Event(), Event()
    original_read = ParquetSource.read_window

    def delayed_read(
        source: ParquetSource, start: int, stop: int, cancelled: Event
    ) -> DataPage:
        started.set()
        release.wait(timeout=5)
        return original_read(source, start, stop, cancelled)

    app = ParqxApp([small_parquet])
    with patch.object(ParquetSource, "read_window", delayed_read):
        async with app.run_test() as pilot:
            try:
                await wait_for(started.is_set, pilot)
                table = app.query_one(ArrowTable)
                assert table.data.peek(0, 0) is None
                await pilot.press("down", "right", "enter")
                assert table.cursor_coordinate == Coordinate(1, 1)
                focused: Widget
                if overlay == "query":
                    query = await open_query(app, pilot)
                    focused = query.editor
                else:
                    await pilot.press("ctrl+p")
                    assert isinstance(app.screen, CommandPalette)
                    focused = app.screen.query_one(Input)
                screen = app.screen
                release.set()
                await wait_for(lambda: table.data.peek(0, 0) is not None, pilot)
                assert app.screen is screen
                assert focused.has_focus
                assert table.cursor_coordinate == Coordinate(1, 1)
            finally:
                release.set()


@pytest.mark.parametrize("stale_error", [False, True])
async def test_closed_source_cancels_window_and_releases_cache(
    small_parquet: Path, stale_error: bool
) -> None:
    started, release, returned = Event(), Event(), Event()
    cancellation: list[Event] = []

    def delayed_read(
        source: ParquetSource, start: int, stop: int, cancelled: Event
    ) -> DataPage:
        cancellation.append(cancelled)
        started.set()
        release.wait(timeout=5)
        try:
            if stale_error:
                raise OSError("obsolete window failed")
            return DataPage(
                start, pa.table({"id": [1], "name": ["alice"], "score": [1.5]})
            )
        finally:
            returned.set()

    app = ParqxApp([small_parquet])
    with patch.object(ParquetSource, "read_window", delayed_read):
        async with app.run_test() as pilot:
            try:
                await wait_for(started.is_set, pilot)
                source_table = app.query_one(ArrowTable)
                cached = weakref.ref(source_table.data)
                tabs = app.query_one(TabbedContent)
                result = await run_query(app, pilot, "SELECT 42 AS answer")
                await select_tab(tabs, "source-1", pilot)
                await pilot.press("ctrl+w")
                await wait_for(lambda: not tabs.query("#source-1"), pilot)
                assert all(event.is_set() for event in cancellation)

                def released() -> bool:
                    gc.collect()
                    return cached() is None

                await wait_for(released, pilot)
                release.set()
                await wait_for(returned.is_set, pilot)
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                assert tabs.tab_count == 1
                assert tabs.active == "query-1"
                assert result.get_cell_at(Coordinate(0, 0)).as_py() == 42
                assert not app.load_errors
            finally:
                release.set()


async def test_window_error_waits_for_navigation_before_retry(tmp_path: Path) -> None:
    path = tmp_path / "retry.parquet"
    pq.write_table(pa.table({"n": range(1_000)}), path, row_group_size=100)
    original_read = ParquetSource.read_window
    calls = 0

    def fail_first_read(
        source: ParquetSource, start: int, stop: int, cancelled: Event
    ) -> DataPage:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("window temporarily unavailable")
        return original_read(source, start, stop, cancelled)

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
        source: ParquetSource, start: int, stop: int, cancelled: Event
    ) -> DataPage:
        cancellation.append(cancelled)
        started.set()
        cancelled.wait(timeout=5)
        try:
            return original_read(source, start, stop, cancelled)
        finally:
            finished.set()

    app = ParqxApp([small_parquet])
    with patch.object(ParquetSource, "read_window", wait_for_cancel):
        async with app.run_test() as pilot:
            await wait_for(started.is_set, pilot)
    assert all(event.is_set() for event in cancellation)
    assert finished.wait(timeout=2)
