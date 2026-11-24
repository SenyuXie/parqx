"""Timer and executor wiring for a source pane's read slot."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import pyarrow as pa
from textual.app import App, ComposeResult
from textual.timer import Timer

from parqx.catalog import SourceCatalog
from parqx.config import ParqxConfig
from parqx.data.parquet import ParquetSource
from parqx.data.view import DataPage, PageCache
from parqx.tui.sources.pane import SourcePane
from parqx.tui.table.arrow_table import ArrowTable
from tests.helpers import wait_for


async def test_queued_read_reserves_the_slot_before_native_work_starts(
    small_parquet: Path,
) -> None:
    source = ParquetSource(
        small_parquet, page_rows=2, page_bytes=1024, memory_limit="128MB", threads=1
    )
    spec = SourceCatalog([small_parquet]).entries[0].spec
    pool_release = Event()
    reads: list[int] = []

    def read_window(source: ParquetSource, start: int, stop: int) -> DataPage:
        reads.append(start)
        if len(reads) == 1:
            raise OSError("obsolete window")
        return DataPage(start, pa.table({"id": [42]}))

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(pool_release.wait, 10)
        pane = SourcePane(
            "Source",
            spec,
            id="source",
            executor=executor,
            config=ParqxConfig(page_debounce_seconds=0.25),
        )
        # Each viewport request below comes from the test instead of rendering.
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
                    cache = table.data
                    assert isinstance(cache, PageCache)
                    with patch.object(
                        pane, "set_timer", return_value=Mock(spec=Timer)
                    ) as timer:
                        for start in (0, 2, 4):
                            pane.post_message(
                                ArrowTable.WindowRequested(
                                    table, cache, start, start + 1
                                )
                            )
                            await pilot.pause()
                            assert timer.call_args.args[0] == 0.25
                            timer.call_args.args[1]()
                            await pilot.pause()
                        submit.assert_called_once()
                        assert not reads
                        pool_release.set()
                        await wait_for(lambda: cache.peek(4, 0) is not None, pilot)
                        assert reads == [0, 4]
                        notify.assert_not_called()

                        pane.post_message(
                            ArrowTable.WindowRequested(table, cache, 2, 3)
                        )
                        await pilot.pause()
                        timer.return_value.stop.reset_mock()
                        await pane.remove()
                        timer.return_value.stop.assert_called_once()
                        timer.call_args.args[1]()
                        await pilot.pause()
                        assert reads == [0, 4]
            finally:
                pool_release.set()
