"""A source tab and its background page reads."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Executor

from textual import on, work
from textual.app import ComposeResult
from textual.content import Content
from textual.timer import Timer
from textual.widgets import Static, TabPane

from parqx.catalog import SourceIssue, SourceSpec
from parqx.config import ParqxConfig
from parqx.data.parquet import ParquetSource
from parqx.data.view import DataPage, PageCache
from parqx.tui.sources.read_schedule import ReadContext, ReadRequest, SourceReadSchedule
from parqx.tui.table.arrow_table import ArrowTable
from parqx.tui.table.view import TableView
from parqx.tui.values.selection import BrowseRoot

logger = logging.getLogger(__name__)


class SourcePane(TabPane):
    """Page through a Parquet source once the app supplies its metadata.

    Each pane owns its table, page workers and cached data. It runs the reads
    reserved by its schedule and delivers their outcomes on the UI thread.
    The app owns source metadata loading and the catalog, which outlive tabs.
    """

    DEFAULT_CSS = """
    SourcePane {
        height: 1fr;
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
        spec: SourceSpec,
        *,
        id: str,
        config: ParqxConfig,
        executor: Executor | None = None,
    ) -> None:
        """Create a source tab awaiting metadata.

        Args:
            title: Plain-text tab title.
            spec: Identity of the source owned by this tab.
            config: Shared application budgets for page caching and scheduling.
            id: The pane's ID in its tabbed container.
            executor: Pool for source page reads; None uses the default executor.
        """
        super().__init__(Content(title), id=id)
        self._spec = spec
        self._config = config
        self._executor = executor
        self._source: ParquetSource | None = None
        self._page_timer: Timer | None = None
        self._schedule = SourceReadSchedule()
        self.view: TableView | None = None
        self.loading = True
        self._error = Static("", classes="source-error", markup=False)
        self._error.display = False

    @property
    def table(self) -> ArrowTable | None:
        """The source table once its metadata is available."""
        return self.view.table if self.view is not None else None

    def compose(self) -> ComposeResult:
        """Yield available data and the source error placeholder."""
        if self.view is not None:
            yield self.view
        yield self._error

    async def set_source(self, source: ParquetSource) -> None:
        """Attach loaded metadata and enable background reads for a source tab."""
        self._stop_timer()
        self._schedule.invalidate_window()
        self._source = source
        self._error.display = False
        data = PageCache(
            source.schema, source.row_count, cache_bytes=self._config.page_cache_bytes
        )
        if self.view is None:
            self.view = TableView(data)
            await self.mount(self.view)
        else:
            self.view.replace_data(data)
        self.loading = False

    def show_error(self, issue: SourceIssue) -> None:
        """Replace the loading cover with this source's opening failure."""
        self.loading = False
        self._error.update(f"Open error: {issue}")
        self._error.tooltip = Content(str(issue))
        self._error.display = True

    def _read_context(self) -> ReadContext | None:
        """Borrow the currently displayed source data and pending selection."""
        if (
            not self.is_mounted
            or self._source is None
            or self.view is None
            or not isinstance(self.view.table.data, PageCache)
        ):
            return None
        return ReadContext(self._source, self.view.table.data, self.view.pending_root)

    @on(ArrowTable.WindowRequested)
    def _on_window_requested(self, event: ArrowTable.WindowRequested) -> None:
        if event.control is not self.table or event.data is not event.control.data:
            return
        event.stop()
        context = self._read_context()
        if context is None:
            return
        self._stop_timer()
        if event.start_row == event.stop_row:
            self._schedule.invalidate_window()
            return
        window_id = self._schedule.request_window(
            event.start_row, event.stop_row, context
        )
        if self._config.page_debounce_seconds:
            self._page_timer = self.set_timer(
                self._config.page_debounce_seconds, lambda: self._page_ready(window_id)
            )
        else:
            self._page_ready(window_id)

    def _stop_timer(self) -> None:
        if self._page_timer is not None:
            self._page_timer.stop()
            self._page_timer = None

    def _page_ready(self, window_id: int) -> None:
        if self._schedule.window_ready(window_id):
            self._page_timer = None
            self._submit_pending()

    @on(TableView.ValueRequested)
    def _on_value_requested(self, event: TableView.ValueRequested) -> None:
        event.stop()
        if self.view is event.view and event.view.pending_root is event.root:
            self._submit_pending()

    def _submit_pending(self) -> None:
        context = self._read_context()
        if context is not None:
            request = self._schedule.take_next(context)
            if request is not None:
                self._read_page(request)

    @work(group="page", description="Read source page")
    async def _read_page(self, request: ReadRequest) -> None:
        # Cancelling this waiter during unmount does not finish the native read.
        outcome: DataPage | Exception
        try:
            outcome = await asyncio.get_running_loop().run_in_executor(
                self._executor, request.source.read_window, request.start, request.stop
            )
        except Exception as exc:
            outcome = exc

        delivery = self._schedule.finish(
            request,
            self._read_context(),
            outcome if isinstance(outcome, DataPage) else None,
        )
        view = self.view
        if delivery != "discard" and view is not None:
            if isinstance(outcome, Exception):
                message = str(
                    SourceIssue(self._spec, str(outcome) or type(outcome).__name__)
                )
                if isinstance(delivery, BrowseRoot):
                    view.fail_selection(delivery, message)
                else:
                    logger.error(
                        "Failed to read parquet window: %s",
                        request.source.path,
                        exc_info=outcome,
                    )
                    view.table.fail_window()
                    self.notify(
                        message, title="Read error", severity="error", markup=False
                    )
            elif isinstance(delivery, BrowseRoot):
                view.resolve_selection(delivery, outcome)
            else:
                view.table.accept_page(outcome)
        # Delivery may have cleared the pending root. Sample the view again.
        self._submit_pending()

    def on_unmount(self) -> None:
        """Retire pending page reads when the source tab closes."""
        # Textual cancels this pane's workers when it is removed.
        self._stop_timer()
        self._schedule.invalidate_window()
        self._source = None
        self.view = None
