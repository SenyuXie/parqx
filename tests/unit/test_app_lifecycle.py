"""Application construction and query shutdown without mounting the UI."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
from unittest.mock import patch

import pytest

from parqx.catalog import SourceCatalog
from parqx.query.engine import QueryCancelledError, QueryControl, QuerySession
from parqx.tui.app import ParqxApp
from parqx.tui.screens.query import QueryScreen


def test_empty_source_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="At least one"):
        ParqxApp([])


def screen_with_controls(*controls: QueryControl) -> QueryScreen:
    screen = QueryScreen(SourceCatalog([]))
    screen._query_controls.extend(controls)  # pyright: ignore[reportPrivateUsage]
    return screen


async def test_shutdown_budget_applies_when_default_executor_is_saturated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(QueryScreen, "_SHUTDOWN_TIMEOUT", 0.05)
    control = QueryControl()
    release = Event()

    def native_query() -> None:
        control.started.set()
        try:
            release.wait(timeout=5)
        finally:
            control.finished.set()

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        loop.set_default_executor(executor)
        execution = loop.run_in_executor(None, native_query)
        try:
            assert control.started.wait(timeout=1)
            screen = screen_with_controls(control)
            await asyncio.wait_for(screen.on_unmount(), timeout=0.5)
            assert control.cancelled.is_set()
            assert not control.finished.is_set()
            assert not execution.done()
        finally:
            release.set()
            await execution


async def test_unfinished_queries_share_one_shutdown_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(QueryScreen, "_SHUTDOWN_TIMEOUT", 0.05)
    controls = [QueryControl() for _ in range(12)]
    for control in controls:
        control.started.set()
    screen = screen_with_controls(*controls)
    try:
        # A timeout per query would need at least 0.6 seconds for these controls.
        await asyncio.wait_for(screen.on_unmount(), timeout=0.3)
        assert all(control.cancelled.is_set() for control in controls)
        assert not any(control.finished.is_set() for control in controls)
    finally:
        # Release a faulty implementation's executor waits as well.
        for control in controls:
            control.finished.set()


async def test_cancelled_query_starting_after_shutdown_creates_no_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(QueryScreen, "_SHUTDOWN_TIMEOUT", 0.05)
    control = QueryControl()
    occupied, release = Event(), Event()

    def occupy_executor() -> None:
        occupied.set()
        release.wait(timeout=5)

    def late_query() -> None:
        control.started.set()
        try:
            with (
                pytest.raises(QueryCancelledError),
                QuerySession((), "SELECT 42", control),
            ):
                pass
        finally:
            control.finished.set()

    loop = asyncio.get_running_loop()
    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        patch("parqx.data.duckdb.TemporaryDirectory") as temporary,
        patch("parqx.data.duckdb.duckdb.connect") as connect,
    ):
        loop.set_default_executor(executor)
        occupying = loop.run_in_executor(None, occupy_executor)
        execution: asyncio.Future[None] | None = None
        try:
            assert occupied.wait(timeout=1)
            execution = loop.run_in_executor(None, late_query)
            screen = screen_with_controls(control)
            await asyncio.wait_for(screen.on_unmount(), timeout=0.5)
            assert control.cancelled.is_set()
            assert not control.started.is_set()
            assert not execution.done()

            release.set()
            await occupying
            await execution
            assert control.finished.is_set()
            temporary.assert_not_called()
            connect.assert_not_called()
        finally:
            release.set()
            await occupying
            if execution is not None:
                await execution


async def test_shutdown_waits_for_cleanup_and_returns_before_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(QueryScreen, "_SHUTDOWN_TIMEOUT", 0.5)
    control = QueryControl()
    release = Event()

    def native_query() -> None:
        control.started.set()
        try:
            release.wait(timeout=5)
        finally:
            control.finished.set()

    execution = Thread(target=native_query)
    execution.start()
    shutdown: asyncio.Task[None] | None = None
    try:
        assert control.started.wait(timeout=1)
        screen = screen_with_controls(control)
        shutdown = asyncio.create_task(screen.on_unmount())
        await asyncio.sleep(0)
        assert control.cancelled.is_set()
        assert not shutdown.done()
        assert not control.finished.is_set()

        release.set()
        await asyncio.wait_for(shutdown, timeout=0.2)
        assert control.finished.is_set()
    finally:
        release.set()
        execution.join(timeout=1)
        assert not execution.is_alive()
        if shutdown is not None:
            await shutdown
