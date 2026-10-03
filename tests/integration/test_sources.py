"""Multi-source catalog, browsing isolation, and shared SQL lifecycle."""

import gc
import weakref
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
from textual.coordinate import Coordinate
from textual.widgets import Static, TabbedContent

from parqx.data.parquet import ParquetSource
from parqx.data.view import DataPage
from parqx.query.engine import QuerySession
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable, ResultPane
from tests.helpers import (
    open_query,
    run_query,
    select_tab,
    wait_for,
    wait_for_query_error,
)


async def test_sources_keep_order_labels_state_and_sql_after_tabs_close(
    tmp_path: Path,
) -> None:
    users = tmp_path / "one" / "sales report.parquet"
    orders = tmp_path / "two" / users.name
    for path in (users, orders):
        path.parent.mkdir()
    pq.write_table(pa.table({"id": [1, 2], "name": ["alice", "bob"]}), users)
    pq.write_table(pa.table({"user_id": [1, 1, 2], "amount": [10, 15, 7]}), orders)
    app = ParqxApp([orders, users, orders])
    async with app.run_test(size=(120, 24)) as pilot:
        tabs = app.query_one(TabbedContent)
        await wait_for(lambda: len(tabs.query(ArrowTable)) == 2, pilot)
        assert tabs.active == "source-1"
        assert [pane.id for pane in tabs.query(ResultPane)] == ["source-1", "source-2"]
        specs = app.catalog.snapshot()
        assert [spec.path for spec in specs] == [orders.resolve(), users.resolve()]
        labels: list[str] = []
        for spec in specs:
            tab = tabs.get_tab(spec.source_id)
            assert f"SQL: {spec.quoted_name}" in str(tab.tooltip)
            assert str(spec.path) in str(tab.tooltip)
            assert spec.quoted_name in str(tab.label)
            labels.append(str(tab.label))
        assert labels[0] != labels[1]
        first = tabs.get_pane("source-1").query_one(ArrowTable)
        second = tabs.get_pane("source-2").query_one(ArrowTable)

        first.focus()
        await pilot.press("h", "i", "z", "c")
        first.move_cursor(row=2, column=1, animate=False)
        await pilot.pause()
        state = (
            first.show_header,
            first.show_row_index,
            first.zebra_stripes,
            first.cursor_type,
            first.cursor_coordinate,
        )
        await select_tab(tabs, "source-2", pilot)
        assert second.show_header
        assert second.show_row_index
        assert not second.zebra_stripes
        assert second.cursor_type == "cell"
        assert second.cursor_coordinate == Coordinate(0, 0)

        sql = (
            f"SELECT u.name, sum(o.amount) FROM {specs[1].quoted_name} u "
            f"JOIN {specs[0].quoted_name} o ON u.id = o.user_id "
            "GROUP BY u.name ORDER BY u.name"
        )
        result = await run_query(app, pilot, sql)
        assert result.row_count == 2
        assert [
            [result.get_cell_at(Coordinate(row, column)).as_py() for column in range(2)]
            for row in range(2)
        ] == [["alice", 25], ["bob", 7]]
        await select_tab(tabs, "source-1", pilot)
        assert (
            first.show_header,
            first.show_row_index,
            first.zebra_stripes,
            first.cursor_type,
            first.cursor_coordinate,
        ) == state

        # Closing browsing views must retain their SQL catalog entries.
        for source_id in ("source-1", "source-2"):
            await select_tab(tabs, source_id, pilot)
            await pilot.press("ctrl+w")

            def source_closed(pane_id: str = source_id) -> bool:
                return not tabs.query(f"#{pane_id}")

            await wait_for(source_closed, pilot)
        assert tabs.tab_count == 1
        assert tabs.active == "query-1"
        assert len(app.catalog.snapshot()) == 2
        rerun = await run_query(app, pilot, sql)
        assert rerun.get_cell_at(Coordinate(0, 1)).as_py() == 25
        assert tabs.tab_count == 2
        assert not tabs.query("#source-1, #source-2")


async def test_partial_failures_remain_visible_and_healthy_source_is_queryable(
    small_parquet: Path, tmp_path: Path
) -> None:
    corrupt, missing = tmp_path / "corrupt.parquet", tmp_path / "missing.parquet"
    directory = tmp_path / "directory.parquet"
    corrupt.write_text("not a parquet file", encoding="utf-8")
    directory.mkdir()
    app = ParqxApp([small_parquet, corrupt, missing, directory])
    async with app.run_test(size=(100, 24)) as pilot:
        tabs = app.query_one(TabbedContent)
        await wait_for(
            lambda: all(entry.state != "loading" for entry in app.catalog.entries),
            pilot,
        )
        assert app.is_running
        assert tabs.tab_count == 4
        assert [entry.state for entry in app.catalog.entries] == [
            "ready",
            "failed",
            "failed",
            "failed",
        ]
        assert {issue.source.path for issue in app.load_errors} == {
            corrupt.resolve(),
            missing.resolve(),
            directory.resolve(),
        }
        for issue in app.load_errors:
            pane = tabs.get_pane(issue.source.source_id)
            assert not pane.loading
            assert not pane.query(ArrowTable)
            error = pane.query_one(".source-error", Static)
            assert error.display
            assert str(issue.source.path) in str(error.content)
            assert issue.message in str(error.content)
        result = await run_query(app, pilot, 'SELECT count(*) FROM "smoke"')
        assert result.get_cell_at(Coordinate(0, 0)).as_py() == 5


async def test_all_failed_sources_exit_with_failure(tmp_path: Path) -> None:
    corrupt, missing = tmp_path / "corrupt.parquet", tmp_path / "missing.parquet"
    corrupt.write_text("invalid parquet", encoding="utf-8")
    app = ParqxApp([corrupt, missing])
    async with app.run_test() as pilot:
        await wait_for(lambda: app.return_code == 1, pilot)
    assert app.return_code == 1
    assert len(app.load_errors) == 2
    assert all(entry.state == "failed" for entry in app.catalog.entries)


async def test_pending_windows_and_close_are_isolated_between_sources(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first.parquet", tmp_path / "second.parquet"
    pq.write_table(pa.table({"n": range(1_000)}), first, row_group_size=100)
    pq.write_table(pa.table({"n": range(10_000, 11_000)}), second, row_group_size=100)
    first_started, first_release, first_returned = Event(), Event(), Event()
    second_started, second_release = Event(), Event()
    first_cancellations: list[Event] = []
    second_cancellations: list[Event] = []
    original_read = ParquetSource.read_window

    def delayed_read(
        source: ParquetSource, start: int, stop: int, cancelled: Event
    ) -> DataPage:
        if source.path == first.resolve():
            first_cancellations.append(cancelled)
            first_started.set()
            try:
                assert first_release.wait(timeout=15)
                # Deliver a stale successful read even after cancellation.
                return DataPage(start, pa.table({"n": [-1]}))
            finally:
                first_returned.set()
        if start >= 500:
            second_cancellations.append(cancelled)
            second_started.set()
            assert second_release.wait(timeout=15)
        return original_read(source, start, stop, cancelled)

    app = ParqxApp([first, second])
    with (
        patch.object(ParquetSource, "read_window", delayed_read),
        patch.object(app, "notify", wraps=app.notify) as notify,
    ):
        async with app.run_test(size=(80, 18)) as pilot:
            try:
                tabs = app.query_one(TabbedContent)
                await wait_for(first_started.is_set, pilot)
                await wait_for(lambda: len(tabs.query(ArrowTable)) == 2, pilot)
                first_cache = weakref.ref(
                    tabs.get_pane("source-1").query_one(ArrowTable).data
                )
                second_table = tabs.get_pane("source-2").query_one(ArrowTable)
                await select_tab(tabs, "source-2", pilot)
                await wait_for(lambda: second_table.data.peek(0, 0) is not None, pilot)
                assert second_table.get_cell_at(Coordinate(0, 0)).as_py() == 10_000
                assert not first_returned.is_set()
                assert not any(event.is_set() for event in first_cancellations)

                second_table.focus()
                await pilot.press("ctrl+end")
                await wait_for(second_started.is_set, pilot)
                pending_second = second_cancellations[-1]
                assert not pending_second.is_set()
                await select_tab(tabs, "source-1", pilot)
                await pilot.press("ctrl+w")
                await wait_for(lambda: not tabs.query("#source-1"), pilot)
                assert all(event.is_set() for event in first_cancellations)
                assert not pending_second.is_set()

                def cache_released() -> bool:
                    gc.collect()
                    return first_cache() is None

                await wait_for(cache_released, pilot)
                first_release.set()
                await wait_for(first_returned.is_set, pilot)
                await pilot.pause()
                assert not tabs.query("#source-1")
                assert second_table.data.peek(999, 0) is None
                assert not pending_second.is_set()
                second_release.set()
                await wait_for(
                    lambda: second_table.data.peek(999, 0) is not None, pilot
                )
                assert second_table.get_cell_at(Coordinate(999, 0)).as_py() == 10_999
                assert not app.load_errors
                notify.assert_not_called()
            finally:
                first_release.set()
                second_release.set()


async def test_closing_source_cancels_queued_page_without_reading_it(
    tmp_path: Path,
) -> None:
    paths = [tmp_path / f"file-{index}.parquet" for index in range(5)]
    for path in paths:
        pq.write_table(pa.table({"n": [1, 2, 3]}), path)
    release, queued = Event(), Event()
    started = {path.resolve(): Event() for path in paths}
    cancellations: list[Event] = []
    queued_cancelled: list[Callable[[], bool]] = []
    original_read = ParquetSource.read_window
    original_submit = ThreadPoolExecutor.submit

    def delayed_read(
        source: ParquetSource, start: int, stop: int, cancelled: Event
    ) -> DataPage:
        cancellations.append(cancelled)
        started[source.path].set()
        assert release.wait(timeout=15)
        return original_read(source, start, stop, cancelled)

    def track_submit[T, **P](
        executor: ThreadPoolExecutor,
        fn: Callable[P, T],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Future[T]:
        future = original_submit(executor, fn, *args, **kwargs)
        source = getattr(fn, "__self__", None)
        if isinstance(source, ParquetSource) and source.path == paths[-1].resolve():
            queued_cancelled.append(future.cancelled)
            queued.set()
        return future

    app = ParqxApp(paths)
    with (
        patch.object(ParquetSource, "read_window", delayed_read),
        patch.object(ThreadPoolExecutor, "submit", track_submit),
    ):
        async with app.run_test(size=(140, 20)) as pilot:
            try:
                await wait_for(lambda: len(app.catalog.snapshot()) == 5, pilot)
                tabs = app.query_one(TabbedContent)
                await wait_for(started[paths[0].resolve()].is_set, pilot)
                # Occupy every source-reading thread before requesting the last tab.
                for index in range(1, 4):
                    await select_tab(tabs, f"source-{index + 1}", pilot)
                    await wait_for(started[paths[index].resolve()].is_set, pilot)
                await select_tab(tabs, "source-5", pilot)
                await wait_for(queued.is_set, pilot)
                assert not started[paths[-1].resolve()].is_set()
                assert not any(is_cancelled() for is_cancelled in queued_cancelled)

                await pilot.press("ctrl+w")
                await wait_for(lambda: not tabs.query("#source-5"), pilot)
                await wait_for(
                    lambda: all(is_cancelled() for is_cancelled in queued_cancelled),
                    pilot,
                )
                assert not any(event.is_set() for event in cancellations)
                release.set()
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                assert not started[paths[-1].resolve()].is_set()
                assert tabs.tab_count == 4
            finally:
                release.set()


async def test_metadata_pool_is_bounded_and_does_not_block_sql(tmp_path: Path) -> None:
    paths = [tmp_path / f"file-{index}.parquet" for index in range(5)]
    for path in paths:
        pq.write_table(pa.table({"n": [1]}), path)
    release = Event()
    lock = Lock()
    active = peak = opened = 0

    def counts() -> tuple[int, int, int]:
        with lock:
            return active, peak, opened

    def delayed_metadata(path: Path) -> ParquetSource:
        nonlocal active, peak, opened
        with lock:
            active += 1
            opened += 1
            peak = max(peak, active)
        try:
            assert release.wait(timeout=15)
            return ParquetSource(path)
        finally:
            with lock:
                active -= 1

    app = ParqxApp(paths)
    with patch("parqx.tui.app.ParquetSource", delayed_metadata):
        async with app.run_test(size=(120, 24)) as pilot:
            try:
                await wait_for(lambda: counts()[0] >= 4, pilot)
                await pilot.pause()
                assert counts() == (4, 4, 4)
                assert not app.catalog.snapshot()
                result = await run_query(app, pilot, "SELECT 42 AS answer")
                assert result.get_cell_at(Coordinate(0, 0)).as_py() == 42
                assert counts() == (4, 4, 4)
                assert not release.is_set()
                assert all(entry.state == "loading" for entry in app.catalog.entries)

                release.set()
                await wait_for(
                    lambda: all(
                        entry.state == "ready" for entry in app.catalog.entries
                    ),
                    pilot,
                )
                assert counts() == (0, 4, 5)
                assert len(app.catalog.snapshot()) == len(paths)
            finally:
                release.set()


async def test_running_query_uses_frozen_sources_until_retried(
    small_parquet: Path,
) -> None:
    pending = small_parquet.with_name("pending.parquet")
    pending.write_bytes(small_parquet.read_bytes())
    metadata_started, metadata_release = Event(), Event()
    query_started, query_release = Event(), Event()
    original_enter = QuerySession.__enter__

    def delayed_metadata(path: Path) -> ParquetSource:
        if path == pending.resolve():
            metadata_started.set()
            assert metadata_release.wait(timeout=15)
        return ParquetSource(path)

    def delayed_enter(session: QuerySession) -> QuerySession:
        query_started.set()
        assert query_release.wait(timeout=15)
        return original_enter(session)

    app = ParqxApp([small_parquet, pending])
    with (
        patch("parqx.tui.app.ParquetSource", delayed_metadata),
        patch.object(QuerySession, "__enter__", delayed_enter),
        patch.object(app, "notify", wraps=app.notify) as notify,
    ):
        async with app.run_test(size=(100, 24)) as pilot:
            try:
                await wait_for(metadata_started.is_set, pilot)
                await wait_for(
                    lambda: app.catalog.get("source-1").state == "ready", pilot
                )
                query = await open_query(app, pilot)
                query.editor.load_text('SELECT count(*) FROM "pending"')
                await pilot.press("enter")
                await wait_for(query_started.is_set, pilot)
                metadata_release.set()
                await wait_for(
                    lambda: app.catalog.get("source-2").state == "ready", pilot
                )
                assert query.running
                assert query.editor.has_focus
                query_release.set()
                message = await wait_for_query_error(notify, query, pilot)
                assert "pending" in message
                assert "Still loading" in message
                assert query.editor.has_focus
                await pilot.press("enter")
                await wait_for(lambda: app.screen is not query, pilot)
                table = app.query_one("#query-1").query_one(ArrowTable)
                assert table.get_cell_at(Coordinate(0, 0)).as_py() == 5
            finally:
                metadata_release.set()
                query_release.set()
