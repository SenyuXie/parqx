"""Deterministic timer/read ordering at the pane's paging boundary."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import pyarrow as pa
import pytest
from textual.app import App, ComposeResult
from textual.timer import Timer

from parqx.catalog import SourceCatalog
from parqx.config import ParqxConfig
from parqx.data.parquet import ParquetSource
from parqx.data.view import DataPage
from parqx.tui.widgets import ArrowTable, TablePane
from tests.helpers import wait_for


@pytest.mark.parametrize(
    ("debounce", "timer_first"), [(0.25, True), (0.25, False), (0, True)]
)
@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("queued", [False, True])
async def test_latest_window_waits_for_both_timer_and_native_completion(
    small_parquet: Path, debounce: float, timer_first: bool, fail: bool, queued: bool
) -> None:
    source = ParquetSource(
        small_parquet, page_rows=2, page_bytes=1024, memory_limit="128MB", threads=1
    )
    spec = SourceCatalog([small_parquet]).entries[0].spec
    started, release, pool_release = Event(), Event(), Event()
    reads: list[int] = []

    def read_window(source: ParquetSource, start: int, stop: int) -> DataPage:
        reads.append(start)
        if len(reads) == 1:
            started.set()
            assert release.wait(timeout=10)
            if fail:
                raise OSError("obsolete window")
            return DataPage(start, pa.table({"id": [-1]}))
        return DataPage(start, pa.table({"id": [42]}))

    with ThreadPoolExecutor(max_workers=1) as executor:
        if queued:
            executor.submit(pool_release.wait, 10)
        pane = TablePane(
            "Source",
            spec,
            id="source",
            executor=executor,
            config=ParqxConfig(page_debounce_seconds=debounce),
        )
        # Hide rendering to control every request through WindowRequested.
        pane.display = False

        class PaneApp(App[None]):
            def compose(self) -> ComposeResult:
                yield pane

        app = PaneApp()
        with (
            patch.object(ParquetSource, "read_window", read_window),
            patch.object(app, "notify") as notify,
            patch.object(executor, "submit", wraps=executor.submit) as submit,
        ):
            try:
                async with app.run_test() as pilot:
                    await pane.set_source(source)
                    table = pane.table
                    assert table is not None
                    with patch.object(
                        pane, "set_timer", return_value=Mock(spec=Timer)
                    ) as timer:
                        pane.post_message(
                            ArrowTable.WindowRequested(table, table.data, 0, 1)
                        )
                        await pilot.pause()
                        if debounce:
                            assert not reads
                            assert timer.call_args.args[0] == debounce
                            timer.call_args.args[1]()
                        if queued:
                            for start in (2, 4):
                                pane.post_message(
                                    ArrowTable.WindowRequested(
                                        table, table.data, start, start + 1
                                    )
                                )
                            await pilot.pause()
                            if debounce:
                                timer.call_args.args[1]()
                            assert not started.is_set()
                            submit.assert_called_once()
                            pool_release.set()
                        await wait_for(started.is_set, pilot)
                        # Return to the original window; identity alone cannot
                        # distinguish its old result from this newer request.
                        for start in (2, 4, 0):
                            pane.post_message(
                                ArrowTable.WindowRequested(
                                    table, table.data, start, start + 1
                                )
                            )
                        await pilot.pause()
                        if debounce:
                            # A stopped timer's queued callback must be harmless.
                            timer.call_args_list[-2].args[1]()
                            if timer_first:
                                timer.call_args.args[1]()
                        await pilot.pause()
                        assert reads == [0]
                        release.set()
                        await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                        if debounce and not timer_first:
                            assert reads == [0]
                            assert table.data.peek(0, 0) is None
                            timer.call_args.args[1]()
                        await wait_for(lambda: table.data.peek(0, 0) is not None, pilot)
                        assert reads == [0, 0]
                        assert table.data.peek(0, 0).as_py() == 42  # type: ignore[union-attr]  # pyright: ignore[reportOptionalMemberAccess]
                        notify.assert_not_called()
            finally:
                pool_release.set()
                release.set()
