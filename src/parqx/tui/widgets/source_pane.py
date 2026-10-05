"""A source tab that owns its loading display and cancellable page reads."""

from __future__ import annotations

import asyncio
import logging
import weakref
from concurrent.futures import Executor

from textual import on, work
from textual.app import ComposeResult
from textual.content import Content
from textual.widgets import Static
from textual.worker import (
    get_current_worker,  # pyright: ignore[reportUnknownVariableType]
)

from parqx.catalog import SourceIssue, SourceSpec
from parqx.data.duckdb import QueryCancelledError, QueryControl
from parqx.data.parquet import ParquetSource
from parqx.data.view import TableData
from parqx.tui.widgets.arrow_table import ArrowTable
from parqx.tui.widgets.table_pane import TablePane

logger = logging.getLogger(__name__)


class SourcePane(TablePane):
    """Keep paging local to one source while its catalog entry lives in the app."""

    DEFAULT_CSS = """
    SourcePane > .source-error {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
        content-align: center middle;
        color: $error;
    }
    """

    def __init__(self, title: str, source: SourceSpec, executor: Executor) -> None:
        """Show a loading placeholder until the app supplies source metadata."""
        super().__init__(title, id=source.source_id)
        self.spec = source
        self._executor = executor
        self._source: ParquetSource | None = None
        self.loading = True
        self._error = Static("", classes="source-error", markup=False)
        self._error.display = False

    def compose(self) -> ComposeResult:
        """Yield the table and source error beneath the loading cover."""
        yield from super().compose()
        yield self._error

    async def set_source(self, source: ParquetSource) -> None:
        """Mount metadata before rendering can request missing windows."""
        self._source = source
        self._error.display = False
        await self._mount_table(TableData(source.schema, source.row_count))
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
            self._read_page(event.start_row, event.stop_row, weakref.ref(event.data))

    @work(exclusive=True, group="page", description="Read source page")
    async def _read_page(
        self, start: int, stop: int, data: weakref.ReferenceType[TableData]
    ) -> None:
        cancelled = get_current_worker().cancelled_event
        source = self._source
        if source is None or cancelled.is_set():
            return
        control = QueryControl(cancelled=cancelled)
        if self._current_table(source, control, data) is None:
            return
        # Native work receives the source and control, never the table or cache.
        prefetch_stop = max(stop, start + source.page_rows)
        try:
            page = await asyncio.get_running_loop().run_in_executor(
                self._executor, source.read_window, start, prefetch_stop, control
            )
        except asyncio.CancelledError:
            control.cancel()
            raise
        except QueryCancelledError:
            return
        except Exception as exc:
            if (table := self._current_table(source, control, data)) is not None:
                logger.exception("Failed to read parquet window: %s", source.path)
                table.fail_window()
                self.notify(
                    str(SourceIssue(self.spec, str(exc) or type(exc).__name__)),
                    title="Read error",
                    severity="error",
                    markup=False,
                )
        else:
            if (table := self._current_table(source, control, data)) is not None:
                table.accept_page(page)

    def _current_table(
        self,
        source: ParquetSource,
        control: QueryControl,
        data: weakref.ReferenceType[TableData],
    ) -> ArrowTable | None:
        if (
            self.is_mounted
            and self._source is source
            and not control.cancelled.is_set()
            and self.table is not None
            and self.table.data is data()
        ):
            return self.table
        return None

    def on_unmount(self) -> None:
        """Release source metadata; Textual cancels the pane's page workers."""
        self._source = None
