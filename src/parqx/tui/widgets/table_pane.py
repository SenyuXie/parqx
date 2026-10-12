"""Table tabs with optional source paging and shared data ownership."""

from __future__ import annotations

import asyncio
import logging
import weakref
from concurrent.futures import Executor

import pyarrow as pa
from textual import on, work
from textual.app import ComposeResult
from textual.content import Content
from textual.timer import Timer
from textual.widgets import Static, TabPane

from parqx.catalog import SourceIssue, SourceSpec
from parqx.config import ParqxConfig
from parqx.data.parquet import ParquetSource
from parqx.data.view import PageCache
from parqx.tui.widgets.arrow_table import ArrowTable

logger = logging.getLogger(__name__)


class TablePane(TabPane):
    """Display loaded data or page through a source supplied by the app.

    Each pane owns its table, page workers and cached data. The app owns source
    metadata loading and the catalog, which outlive individual tabs.
    """

    DEFAULT_CSS = """
    TablePane {
        height: 1fr;
        & > ArrowTable { height: 1fr; }
        & > .source-error {
            width: 1fr;
            height: 1fr;
            padding: 1 2;
            content-align: center middle;
            color: $error;
        }
    }
    """

    def __init__(
        self,
        title: str,
        data: pa.Table | SourceSpec,
        *,
        id: str,
        config: ParqxConfig,
        executor: Executor | None = None,
    ) -> None:
        """Create a table or wait for the app to supply source metadata.

        Args:
            title: Plain-text tab title.
            data: Loaded table data, or a source identity awaiting set_source.
            config: Shared application budgets for page caching and scheduling.
            id: The pane's ID in its tabbed container.
            executor: Pool for source page reads; None uses the default executor.
        """
        super().__init__(Content(title), id=id)
        self._config = config
        self._executor = executor
        self._source: ParquetSource | None = None
        self._page_timer: Timer | None = None
        self._pending_window: (
            tuple[int, int, weakref.ReferenceType[PageCache]] | None
        ) = None
        self._request_number = 0
        self._reading = False
        self._spec: SourceSpec | None = None
        self.table: ArrowTable | None = None
        if isinstance(data, SourceSpec):
            self._spec = data
            self.loading = True
        else:
            self.table = ArrowTable(data)
        self._error = Static("", classes="source-error", markup=False)
        self._error.display = False

    def compose(self) -> ComposeResult:
        """Yield available data and the source error placeholder."""
        if self.table is not None:
            yield self.table
        yield self._error

    async def set_source(self, source: ParquetSource) -> None:
        """Attach loaded metadata and enable background reads for a source tab."""
        if self._spec is None:
            raise ValueError("Only source tabs can load a Parquet source")
        self._clear_pending()
        self._source = source
        self._error.display = False
        data = PageCache(
            source.schema, source.row_count, cache_bytes=self._config.page_cache_bytes
        )
        if self.table is None:
            self.table = ArrowTable(data)
            await self.mount(self.table)
        else:
            self.table.replace_data(data)
        self.loading = False

    def show_error(self, issue: SourceIssue) -> None:
        """Replace the loading cover with this source's opening failure."""
        self.loading = False
        self._error.update(f"Open error: {issue}")
        self._error.tooltip = Content(str(issue))
        self._error.display = True

    @on(ArrowTable.WindowRequested)
    def _on_window_requested(self, event: ArrowTable.WindowRequested) -> None:
        if event.control is not self.table or event.data is not event.control.data:
            return
        event.stop()
        if self.is_mounted and self._source is not None:
            self._clear_pending()
            self._pending_window = (
                event.start_row,
                event.stop_row,
                weakref.ref(event.data),
            )
            if self._config.page_debounce_seconds:
                request = self._request_number
                self._page_timer = self.set_timer(
                    self._config.page_debounce_seconds,
                    lambda: self._page_ready(request),
                )
            else:
                self._submit_pending()

    def _clear_pending(self) -> None:
        self._request_number += 1
        self._pending_window = None
        if self._page_timer is not None:
            self._page_timer.stop()
            self._page_timer = None

    def _page_ready(self, request: int) -> None:
        if request == self._request_number:
            self._page_timer = None
            self._submit_pending()

    def _submit_pending(self) -> None:
        if (
            self._reading
            or self._page_timer is not None
            or self._pending_window is None
        ):
            return
        start, stop, data = self._pending_window
        self._pending_window = None
        source, spec = self._source, self._spec
        request = self._request_number
        if (
            source is None
            or spec is None
            or self._current_table(source, request, data) is None
        ):
            return
        # Reserve the slot before Textual schedules the worker, including pool wait.
        self._reading = True
        self._read_page(
            source, spec, start, max(stop, start + source.page_rows), request, data
        )

    @work(group="page", description="Read source page")
    async def _read_page(
        self,
        source: ParquetSource,
        spec: SourceSpec,
        start: int,
        stop: int,
        request: int,
        data: weakref.ReferenceType[PageCache],
    ) -> None:
        # Native work receives no widget or cache. Unmount can cancel the async
        # wait (and queued work); an already running native read finishes naturally.
        try:
            page = await asyncio.get_running_loop().run_in_executor(
                self._executor, source.read_window, start, stop
            )
        except Exception as exc:
            if (table := self._current_table(source, request, data)) is not None:
                logger.exception("Failed to read parquet window: %s", source.path)
                table.fail_window()
                self.notify(
                    str(SourceIssue(spec, str(exc) or type(exc).__name__)),
                    title="Read error",
                    severity="error",
                    markup=False,
                )
        else:
            if (table := self._current_table(source, request, data)) is not None:
                table.accept_page(page)
        # CancelledError intentionally skips this: cancelling the waiter does not
        # mean native work finished, and must never free a slot for another read.
        self._reading = False
        self._submit_pending()

    def _current_table(
        self,
        source: ParquetSource,
        request: int,
        data: weakref.ReferenceType[PageCache],
    ) -> ArrowTable | None:
        if (
            self.is_mounted
            and self._source is source
            and request == self._request_number
            and self.table is not None
            and self.table.data is data()
        ):
            return self.table
        return None

    def on_unmount(self) -> None:
        """Release Arrow buffers even if Textual briefly retains the closed widget."""
        # Textual cancels this pane's workers when it is removed.
        self._clear_pending()
        self._source = None
        if self.table is not None:
            self.table.replace_data(pa.table({}))
            self.table = None
