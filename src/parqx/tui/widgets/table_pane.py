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
from textual.widgets import Static, TabPane
from textual.worker import (
    get_current_worker,  # pyright: ignore[reportUnknownVariableType]
)

from parqx.catalog import SourceIssue, SourceSpec
from parqx.data.duckdb import QueryCancelledError, QueryControl
from parqx.data.parquet import ParquetSource
from parqx.data.view import TableData
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
        data: pa.Table | TableData | SourceSpec,
        *,
        id: str,
        executor: Executor | None = None,
    ) -> None:
        """Create a table or wait for the app to supply source metadata.

        Args:
            title: Plain-text tab title.
            data: Loaded table data, or a source identity awaiting set_source.
            id: The pane's ID in its tabbed container.
            executor: Pool for source page reads; None uses the default executor.
        """
        super().__init__(Content(title), id=id)
        self._executor = executor
        self._source: ParquetSource | None = None
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
        self._source = source
        self._error.display = False
        data = TableData(source.schema, source.row_count)
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
            self._read_page(event.start_row, event.stop_row, weakref.ref(event.data))

    @work(exclusive=True, group="page", description="Read source page")
    async def _read_page(
        self, start: int, stop: int, data: weakref.ReferenceType[TableData]
    ) -> None:
        cancelled = get_current_worker().cancelled_event
        source = self._source
        spec = self._spec
        if source is None or spec is None or cancelled.is_set():
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
                    str(SourceIssue(spec, str(exc) or type(exc).__name__)),
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
        """Release Arrow buffers even if Textual briefly retains the closed widget."""
        # Textual cancels this pane's workers when it is removed.
        self._source = None
        if self.table is not None:
            self.table.replace_table(pa.table({}))
            self.table = None
