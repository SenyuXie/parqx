"""Query ownership across queued callbacks, cancellation and screen changes."""

from collections.abc import Callable
from pathlib import Path
from threading import Event
from unittest.mock import patch

import duckdb
import pytest
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import TabbedContent

from parqx.query.engine import QueryControl, QuerySession
from parqx.tui.app import ParqxApp
from parqx.tui.screens.query import QueryScreen
from parqx.tui.widgets import ArrowTable
from tests.test_query_app import open_query, run_query, wait_for


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


@pytest.mark.parametrize("stale_error", [False, True])
async def test_queued_callback_cannot_finish_a_new_query(
    small_parquet: Path, query_controls: list[QueryControl], stale_error: bool
) -> None:
    publishing, release_old = Event(), Event()
    current_started, release_current = Event(), Event()
    original_publish = QueryScreen._publish  # pyright: ignore[reportPrivateUsage]
    original_enter = QuerySession.__enter__

    def delayed_publish[T, **P](
        screen: QueryScreen, callback: Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ) -> T | None:
        if not publishing.is_set():
            # The worker has already passed its cancellation check. Its callback
            # must still be rejected after another request takes over the UI.
            publishing.set()
            assert release_old.wait(timeout=10)
        return original_publish(screen, callback, *args, **kwargs)

    def delayed_enter(session: QuerySession) -> QuerySession:
        if session.sql == "SELECT 42 AS current":
            current_started.set()
            assert release_current.wait(timeout=10)
        return original_enter(session)

    app = ParqxApp([small_parquet])
    with (
        patch.object(QueryScreen, "_publish", delayed_publish),
        patch.object(QuerySession, "__enter__", delayed_enter),
        patch.object(app, "notify", wraps=app.notify) as notify,
    ):
        async with app.run_test() as pilot:
            try:
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                tabs = app.query_one(TabbedContent)
                query = await open_query(app, pilot)
                query.editor.load_text(
                    "SELECT missing_obsolete" if stale_error else "SELECT 1 AS obsolete"
                )
                query.action_run_query()
                await wait_for(publishing.is_set, pilot)
                old = query_controls[0]
                assert not old.finished.is_set()

                query.action_close()
                await wait_for(lambda: app.screen is not query, pilot)
                await open_query(app, pilot)
                query.editor.load_text("SELECT 42 AS current")
                query.action_run_query()
                await wait_for(current_started.is_set, pilot)
                current = query_controls[1]
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
                assert tabs.tab_count == 1
                notify.assert_not_called()

                release_current.set()
                await wait_for(
                    lambda: app.screen is not query and tabs.tab_count == 2, pilot
                )
                result = tabs.get_pane("query-1").query_one(ArrowTable)
                assert result.get_cell_at(Coordinate(0, 0)).as_py() == 42
                notify.assert_not_called()
            finally:
                release_old.set()
                release_current.set()


async def test_close_before_query_worker_starts_preserves_next_run(
    small_parquet: Path, query_controls: list[QueryControl]
) -> None:
    app = ParqxApp([small_parquet])
    with (
        patch.object(duckdb, "connect", wraps=duckdb.connect) as connect,
        patch.object(app, "notify", wraps=app.notify) as notify,
    ):
        async with app.run_test() as pilot:
            await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
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
    deliveries: list[Callable[[], object]] = []
    original_publish = QueryScreen._publish  # pyright: ignore[reportPrivateUsage]

    def delayed_publish[T, **P](
        screen: QueryScreen, callback: Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ) -> T | None:
        if not publishing.is_set():
            deliveries.append(lambda: callback(*args, **kwargs))
            publishing.set()
            assert release.wait(timeout=10)
            return None
        return original_publish(screen, callback, *args, **kwargs)

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
