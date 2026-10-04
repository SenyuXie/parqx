"""Query ownership across queued callbacks, cancellation and screen changes."""

from collections.abc import Callable
from pathlib import Path
from threading import Event
from unittest.mock import patch

import duckdb
import pytest
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import Footer, TabbedContent
from textual.worker import WorkerFailed

from parqx.data.parquet import ParquetSource
from parqx.query.engine import QueryControl, QuerySession
from parqx.tui.app import ParqxApp
from parqx.tui.screens.query import QueryScreen
from parqx.tui.widgets import ArrowTable
from tests.helpers import (
    footer_keys,
    footer_ready,
    open_query,
    run_query,
    select_tab,
    wait_for,
    wait_for_query_error,
)


@pytest.fixture
def query_controls(monkeypatch: pytest.MonkeyPatch) -> list[QueryControl]:
    """Record real controls without depending on the screen's current field name."""
    controls: list[QueryControl] = []

    def create_control() -> QueryControl:
        control = QueryControl()
        controls.append(control)
        return control

    monkeypatch.setattr("parqx.tui.screens.query.QueryControl", create_control)
    return controls


@pytest.mark.parametrize(
    ("operation", "failure"),
    [
        ("preview", RuntimeError("unexpected preview failure")),
        ("constructor", MemoryError()),
    ],
)
async def test_worker_failure_unlocks_editor_and_allows_another_query(
    small_parquet: Path,
    query_controls: list[QueryControl],
    caplog: pytest.LogCaptureFixture,
    operation: str,
    failure: Exception,
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        query = await open_query(app, pilot)
        tabs = app.query_one(TabbedContent)
        query.editor.load_text("SELECT 1 AS failed")
        target = (
            "parqx.query.engine.QuerySession.preview"
            if operation == "preview"
            else "parqx.tui.screens.query.QuerySession"
        )
        with (
            patch(target, side_effect=failure),
            patch.object(app, "notify", wraps=app.notify) as notify,
        ):
            query.action_run_query()
            control = query_controls[-1]
            message = await wait_for_query_error(notify, query, pilot)
            await wait_for(control.finished.is_set, pilot)
            assert (str(failure) or type(failure).__name__) in message
            assert control.started.is_set()
            assert not control.cancelled.is_set()
            assert app.screen is query
            assert not query.editor.read_only
            assert not query.query_one("#query-loading").display
            assert query.editor.has_focus
            assert query.editor.text == "SELECT 1 AS failed"
            assert tabs.tab_count == 1

        errors = [
            record
            for record in caplog.records
            if record.name == "parqx.tui.screens.query"
        ]
        if operation == "preview":
            assert len(errors) == 1
            assert errors[0].exc_info is not None
            assert errors[0].exc_info[1] is failure
            assert errors[0].exc_info[2] is not None
        else:
            assert not errors

        result = await run_query(app, pilot, "SELECT 42 AS answer")
        assert result.get_cell_at(Coordinate(0, 0)).as_py() == 42
        await wait_for(query_controls[-1].finished.is_set, pilot)
        assert tabs.tab_count == 2


@pytest.mark.parametrize(
    ("callback", "sql"), [("dismiss", "SELECT 42"), ("notify", "SELECT missing_column")]
)
async def test_ui_delivery_failure_propagates_and_finishes_request(
    small_parquet: Path, query_controls: list[QueryControl], callback: str, sql: str
) -> None:
    app = ParqxApp([small_parquet])
    failure = RuntimeError("UI delivery failed")

    async def run_app() -> None:
        async with app.run_test() as pilot:
            await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
            query = await open_query(app, pilot)
            query.editor.load_text(sql)
            with patch.object(query, callback, side_effect=failure):
                query.action_run_query()
                worker = next(
                    worker for worker in app.workers if worker.group == "query"
                )
                await wait_for(lambda: worker.is_finished, pilot)

    with pytest.raises(WorkerFailed, match="UI delivery failed") as raised:
        await run_app()
    assert raised.value.error is failure
    assert len(query_controls) == 1
    assert query_controls[0].finished.is_set()


async def test_queued_callbacks_cannot_finish_a_new_query(
    small_parquet: Path, query_controls: list[QueryControl]
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        tabs = app.query_one(TabbedContent)

        # Exercise the distinct success/error callbacks in one app lifetime.
        async def check_callback(stale_error: bool) -> None:
            publishing, release_old = Event(), Event()
            current_started, release_current = Event(), Event()
            original_publish = QueryScreen._publish  # pyright: ignore[reportPrivateUsage]
            original_enter = QuerySession.__enter__
            previous_count = tabs.tab_count

            def delayed_publish(
                screen: QueryScreen, callback: Callable[[], None]
            ) -> None:
                if not publishing.is_set():
                    # The worker passed its cancellation check before a new request.
                    publishing.set()
                    assert release_old.wait(timeout=10)
                original_publish(screen, callback)

            def delayed_enter(session: QuerySession) -> QuerySession:
                if session.sql == "SELECT 42 AS current":
                    current_started.set()
                    assert release_current.wait(timeout=10)
                return original_enter(session)

            with (
                patch.object(QueryScreen, "_publish", delayed_publish),
                patch.object(QuerySession, "__enter__", delayed_enter),
                patch.object(app, "notify", wraps=app.notify) as notify,
            ):
                try:
                    query = await open_query(app, pilot)
                    query.editor.load_text(
                        "SELECT missing_obsolete"
                        if stale_error
                        else "SELECT 1 AS obsolete"
                    )
                    query.action_run_query()
                    await wait_for(publishing.is_set, pilot)
                    old = query_controls[-1]
                    assert not old.finished.is_set()
                    query.action_close()
                    await wait_for(lambda: app.screen is not query, pilot)
                    await open_query(app, pilot)
                    query.editor.load_text("SELECT 42 AS current")
                    query.action_run_query()
                    await wait_for(current_started.is_set, pilot)
                    current = query_controls[-1]
                    assert old.cancelled.is_set()

                    release_old.set()
                    await wait_for(old.finished.is_set, pilot)
                    assert app.screen is query
                    assert query.running
                    assert query.editor.read_only
                    assert query.query_one("#query-loading").display
                    assert query.editor.text == "SELECT 42 AS current"
                    assert not current.cancelled.is_set()
                    assert not current.finished.is_set()
                    assert tabs.tab_count == previous_count
                    notify.assert_not_called()
                    release_current.set()
                    await wait_for(
                        lambda: (
                            app.screen is not query
                            and tabs.tab_count == previous_count + 1
                        ),
                        pilot,
                    )
                    assert tabs.active_pane is not None
                    result = tabs.active_pane.query_one(ArrowTable)
                    assert result.get_cell_at(Coordinate(0, 0)).as_py() == 42
                    await wait_for(current.finished.is_set, pilot)
                    notify.assert_not_called()
                finally:
                    release_old.set()
                    release_current.set()

        await check_callback(False)
        await check_callback(True)


async def test_close_before_query_worker_starts_preserves_next_run(
    small_parquet: Path, query_controls: list[QueryControl]
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        table = app.query_one(ArrowTable)
        await wait_for(lambda: table.data.peek(0, 0) is not None, pilot)
        with (
            patch.object(duckdb, "connect", wraps=duckdb.connect) as connect,
            patch.object(app, "notify", wraps=app.notify) as notify,
        ):
            query = await open_query(app, pilot)
            query.editor.load_text("SELECT 1 AS cancelled")
            query.action_run_query()
            old = query_controls[0]
            assert not old.started.is_set()
            # Do not yield to the event loop between submitting and cancelling.
            query.action_close()
            assert old.cancelled.is_set()
            assert not query.running
            await wait_for(old.finished.is_set, pilot)
            connect.assert_not_called()
            assert app.query_one(TabbedContent).tab_count == 1

            result = await run_query(app, pilot, "SELECT 42 AS answer")
            assert result.get_cell_at(Coordinate(0, 0)).as_py() == 42
            connect.assert_called_once()
            notify.assert_not_called()


async def test_covering_query_screen_cancels_request_and_preserves_editor(
    small_parquet: Path, query_controls: list[QueryControl]
) -> None:
    publishing, release = Event(), Event()
    deliveries: list[Callable[[], None]] = []
    original_publish = QueryScreen._publish  # pyright: ignore[reportPrivateUsage]

    def delayed_publish(screen: QueryScreen, callback: Callable[[], None]) -> None:
        if not publishing.is_set():
            deliveries.append(callback)
            publishing.set()
            assert release.wait(timeout=10)
            return
        original_publish(screen, callback)

    app = ParqxApp([small_parquet])
    with (
        patch.object(QueryScreen, "_publish", delayed_publish),
        patch.object(app, "notify", wraps=app.notify) as notify,
    ):
        async with app.run_test() as pilot:
            try:
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                query = await open_query(app, pilot)
                tabs = app.query_one(TabbedContent)
                query.editor.load_text("SELECT 1 AS covered")
                query.action_run_query()
                await wait_for(publishing.is_set, pilot)
                old = query_controls[0]
                cover = Screen[None]()
                mount = app.push_screen(cover)
                # Deliver the queued callback on the UI thread before the pending
                # ScreenSuspend message has invalidated the query request.
                assert query.running
                assert not old.cancelled.is_set()
                deliveries[0]()
                assert app.screen is cover
                assert query.running
                assert tabs.tab_count == 1

                await mount
                await wait_for(old.cancelled.is_set, pilot)
                assert app.screen is cover
                assert not query.running
                release.set()
                await wait_for(old.finished.is_set, pilot)
                notify.assert_not_called()
                await app.pop_screen()
                await wait_for(lambda: query.editor.has_focus, pilot)
                assert app.screen is query
                assert query.editor.text == "SELECT 1 AS covered"
                assert not query.running
                assert not query.editor.read_only
                assert not query.query_one("#query-loading").display

                result = await run_query(app, pilot, "SELECT 42 AS answer")
                assert result.get_cell_at(Coordinate(0, 0)).as_py() == 42
                notify.assert_not_called()
            finally:
                release.set()


async def test_shutdown_finishes_cancelled_cleanup_and_current_query(
    small_parquet: Path, query_controls: list[QueryControl]
) -> None:
    cleanup_started, release_cleanup = Event(), Event()
    current_started = Event()
    original_close = QuerySession.close
    original_enter = QuerySession.__enter__

    def delayed_close(session: QuerySession) -> None:
        if session.sql == "SELECT 1 AS retiring":
            cleanup_started.set()
            assert release_cleanup.wait(timeout=10)
        original_close(session)

    def wait_for_shutdown(session: QuerySession) -> QuerySession:
        if session.sql == "SELECT 2 AS current":
            current_started.set()
            try:
                assert session.control.cancelled.wait(timeout=10)
            finally:
                release_cleanup.set()
        return original_enter(session)

    app = ParqxApp([small_parquet])
    with (
        patch.object(QuerySession, "close", delayed_close),
        patch.object(QuerySession, "__enter__", wait_for_shutdown),
        patch.object(app, "notify", wraps=app.notify) as notify,
    ):
        try:
            async with app.run_test() as pilot:
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                query = await open_query(app, pilot)
                query.editor.load_text("SELECT 1 AS retiring")
                query.action_run_query()
                await wait_for(cleanup_started.is_set, pilot)
                old = query_controls[0]
                query.action_close()
                await wait_for(lambda: app.screen is not query, pilot)
                assert old.cancelled.is_set()
                assert not old.finished.is_set()

                await open_query(app, pilot)
                query.editor.load_text("SELECT 2 AS current")
                query.action_run_query()
                await wait_for(current_started.is_set, pilot)
                assert query.running
                assert not old.finished.is_set()
                assert not query_controls[1].cancelled.is_set()
            assert len(query_controls) == 2
            assert all(control.cancelled.is_set() for control in query_controls)
            assert all(control.finished.is_set() for control in query_controls)
            notify.assert_not_called()
        finally:
            release_cleanup.set()


async def test_running_query_blocks_edits_and_duplicate_execution(
    small_parquet: Path,
) -> None:
    started, release = Event(), Event()
    original_enter = QuerySession.__enter__
    calls = 0

    def delayed_enter(session: QuerySession) -> QuerySession:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=5)
        return original_enter(session)

    app = ParqxApp([small_parquet])
    with patch.object(QuerySession, "__enter__", delayed_enter):
        async with app.run_test() as pilot:
            try:
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                tabs = app.query_one(TabbedContent)
                query = await open_query(app, pilot)
                query.editor.load_text("SELECT 42")
                footer = query.query_one(Footer)
                await wait_for(lambda: footer_ready(footer), pilot)
                await pilot.click(footer_keys(footer)["run_query"])
                await wait_for(started.is_set, pilot)
                await wait_for(
                    lambda: (
                        footer_ready(footer)
                        and footer_keys(footer)["run_query"].has_class("-disabled")
                        and footer_keys(footer)["newline"].has_class("-disabled")
                    ),
                    pilot,
                )
                assert not footer_keys(footer)["close"].has_class("-disabled")
                assert footer_keys(footer)["close"].description == "Close"
                assert query.editor.has_focus
                assert query.editor.read_only
                assert await pilot.click(footer_keys(footer)["run_query"])
                assert await pilot.click(footer_keys(footer)["newline"])
                await pilot.press("enter", "enter", "x", "shift+enter", "backspace")
                await pilot.pause()
                assert calls == 1
                assert query.running
                assert query.editor.text == "SELECT 42"
                assert query.query_one("#query-loading").display
                control = query._current_control  # pyright: ignore[reportPrivateUsage]
                assert control is not None
                await pilot.click(footer_keys(footer)["close"])
                await wait_for(lambda: app.screen is not query, pilot)
                assert control.cancelled.is_set()
                assert not query.running
                assert tabs.tab_count == 1
            finally:
                release.set()


async def test_metadata_completion_cannot_recreate_closed_source_tabs(
    small_parquet: Path,
) -> None:
    failed = small_parquet.with_name("failed.parquet")
    healthy = small_parquet.with_name("healthy.parquet")
    for path in (failed, healthy):
        path.write_bytes(small_parquet.read_bytes())
    started = {path: Event() for path in (small_parquet, failed)}
    release = Event()

    def delayed_read(path: Path) -> ParquetSource:
        if path == healthy:
            return ParquetSource(path)
        started[path].set()
        assert release.wait(timeout=10)
        if path == failed:
            raise OSError("obsolete metadata read failed")
        return ParquetSource(path)

    app = ParqxApp([small_parquet, failed, healthy])
    with patch("parqx.tui.app.ParquetSource", delayed_read):
        async with app.run_test() as pilot:
            try:
                await wait_for(
                    lambda: all(event.is_set() for event in started.values()), pilot
                )
                tabs = app.query_one(TabbedContent)
                assert tabs.get_pane("source-1").loading
                assert tabs.get_pane("source-2").loading
                # Both callbacks arrive after their tabs have gone away.
                await select_tab(tabs, "source-1", pilot)
                await pilot.press("ctrl+w")
                await wait_for(lambda: not tabs.query("#source-1"), pilot)
                await select_tab(tabs, "source-2", pilot)
                await pilot.press("ctrl+w")
                await wait_for(lambda: not tabs.query("#source-2"), pilot)
                release.set()
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                assert tabs.tab_count == 1
                assert tabs.active == "source-3"
                assert len(app.load_errors) == 1
                assert "obsolete metadata read failed" in str(app.load_errors)
                # Successful metadata still registers the source for SQL.
                table = await run_query(app, pilot, "SELECT count(*) FROM smoke")
                assert table.get_cell_at(Coordinate(0, 0)).as_py() == 5
            finally:
                release.set()
