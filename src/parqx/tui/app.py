"""Textual application for Parqx."""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Event
from time import perf_counter
from typing import Any, ClassVar

import duckdb
import pyarrow as pa
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.css.query import NoMatches
from textual.widgets import Button, Footer, Static, TextArea

from parqx.data.parquet import ParquetSource, ReadCancelledError
from parqx.data.view import DataPage, TableData, WindowSource
from parqx.query.engine import (
    QueryCancelledError,
    QueryControl,
    QueryLimits,
    QueryPreview,
    QuerySession,
)
from parqx.tui.widgets import ArrowTable, FileLoading
from parqx.tui.widgets.arrow_table import CursorType
from parqx.tui.widgets.query_panel import QueryPanel

logger = logging.getLogger(__name__)


class ParqxApp(App[Any]):
    """A Textual App for Parqx."""

    CSS = """
    ArrowTable { height: 1fr; }
    #query-status {
        dock: bottom;
        height: auto;
        max-height: 3;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f2", "toggle_query", "SQL", show=True),
        Binding("f5", "run_query", "Run SQL", show=True, priority=True),
        Binding("ctrl+enter", "run_query", show=False, priority=True),
        Binding("escape", "cancel_query", show=False, priority=True),
        Binding("f6", "browse", "Browse", show=False),
        Binding("h", "toggle_header", "Header", show=True),
        Binding("i", "toggle_row_index", "Index", show=True),
        Binding("z", "toggle_zebra", "Zebra", show=True),
        Binding("c", "cycle_cursor_type", "Cursor", show=True),
    ]

    _CURSOR_TYPE_CYCLE: ClassVar[tuple[CursorType, ...]] = (
        "cell",
        "row",
        "column",
        "none",
    )
    """Order in which `action_cycle_cursor_type` advances the cursor type."""

    def __init__(
        self,
        path: Path,
        initial_sql: str | None = None,
        query_limits: QueryLimits | None = None,
    ) -> None:
        """Initialize the app with a Parquet file path to inspect.

        Args:
            path: Parquet file shown by the main table widget. The file is read
                asynchronously after the UI mounts, not in this constructor.
            initial_sql: Optional SQL to run directly instead of loading the source.
            query_limits: Optional preview and execution budget overrides.
        """
        super().__init__()
        self._path = path
        self._initial_sql = initial_sql
        self._query_limits = query_limits or QueryLimits()
        self._request_id = 0
        self._query_control: QueryControl | None = None
        self.query_running = False
        self.query_error: str | None = None
        self._window_source: WindowSource | None = None
        self._page_cancelled = Event()
        self._page_request = 0
        self.load_error: str | None = None
        """Set when the worker thread fails to read the file. The CLI inspects
        this after `run` returns to decide between a clean exit and a non-zero
        exit with an error message."""

    def compose(self) -> ComposeResult:
        """Yield the loading placeholder plus a persistent footer.

        The body widget (`FileLoading`, later swapped for `ArrowTable`) is the
        only thing that gets mounted/removed. `Footer` is docked to the bottom
        and persists for the app's lifetime so its key hints are always visible.
        """
        yield FileLoading(self._path)
        yield QueryPanel(self._initial_sql or "SELECT * FROM data")
        yield Static("", id="query-status", markup=False)
        yield Footer(show_command_palette=True)

    def on_mount(self) -> None:
        """Kick off the parquet read as soon as the loading UI is visible."""
        self.query_one(QueryPanel).display = self._initial_sql is not None
        if self._initial_sql is not None:
            self.action_run_query()
        else:
            self.action_browse()

    @work(thread=True, group="load", exclusive=True, exit_on_error=False)
    def _load_table(self, request_id: int) -> None:
        try:
            source = ParquetSource(self._path)
        except (OSError, pa.ArrowException, MemoryError) as exc:
            logger.exception("Failed to read parquet file: %s", self._path)
            self.call_from_thread(self._on_load_error, str(exc), request_id)
            return
        self.call_from_thread(self._on_load_ok, source, request_id)

    def _show_table(
        self, table: pa.Table | TableData, source: WindowSource | None = None
    ) -> None:
        self._page_cancelled.set()
        self._page_request += 1
        self._window_source = source
        if (widget := self._get_arrow_table()) is not None:
            if isinstance(table, TableData):
                widget.replace_data(table)
            else:
                widget.replace_table(table)
        else:
            self.query(FileLoading).remove()
            widget = ArrowTable(table)
            self.mount(widget, after=self.query_one(QueryPanel))
        widget.focus()

    def _on_load_ok(self, source: ParquetSource, request_id: int) -> None:
        if request_id != self._request_id:
            return
        self._show_table(
            TableData(source.schema, source.row_count, source.row_count), source
        )
        self._status(f"{self._path.name} · {source.row_count:,} rows · original values")

    def on_arrow_table_window_requested(
        self, event: ArrowTable.WindowRequested
    ) -> None:
        """Read the current viewport outside of render and navigation callbacks."""
        widget = self._get_arrow_table()
        if (
            widget is None
            or widget.data is not event.data
            or self._window_source is None
        ):
            return
        self._page_cancelled.set()
        self._page_cancelled = cancelled = Event()
        self._page_request += 1
        self._read_page(
            self._window_source,
            event.data,
            event.start_row,
            event.stop_row,
            self._page_request,
            cancelled,
        )

    @work(thread=True, group="page", exclusive=True, exit_on_error=False)
    def _read_page(
        self,
        source: WindowSource,
        data: TableData,
        start: int,
        stop: int,
        request: int,
        cancelled: Event,
    ) -> None:
        try:
            page = source.read_window(start, stop, cancelled)
        except ReadCancelledError:
            return
        except (OSError, pa.ArrowException, MemoryError) as exc:
            if not cancelled.is_set():
                self.call_from_thread(self._on_page_error, request, str(exc))
            return
        if not cancelled.is_set():
            self.call_from_thread(self._on_page_loaded, data, page, request)

    def _on_page_loaded(self, data: TableData, page: DataPage, request: int) -> None:
        widget = self._get_arrow_table()
        if request == self._page_request and widget is not None and widget.data is data:
            widget.accept_page(page)

    def _on_page_error(self, request: int, message: str) -> None:
        if request == self._page_request:
            self._status(f"Read error: {message}")

    def _on_load_error(self, message: str, request_id: int) -> None:
        if request_id != self._request_id:
            return
        self.load_error = message
        self.exit(return_code=1)

    def _status(self, message: str) -> None:
        self.query_one("#query-status", Static).update(message)

    def _new_request(self) -> int:
        if self._query_control is not None:
            self._query_control.cancel()
        self._query_control = None
        self._request_id += 1
        self.query_running = False
        self.query_error = None
        return self._request_id

    def action_toggle_query(self) -> None:
        """Show or hide the SQL editor without discarding the current result."""
        panel = self.query_one(QueryPanel)
        panel.display = not panel.display
        if panel.display:
            self.query_one(TextArea).focus()
        elif (table := self._get_arrow_table()) is not None:
            table.focus()

    def action_browse(self) -> None:
        """Return to the source file's original Arrow values."""
        request_id = self._new_request()
        self._status(f"Loading {self._path.name}…")
        self._load_table(request_id)

    def action_run_query(self) -> None:
        """Submit the editor's SQL on a fresh, cancellable worker."""
        request_id = self._new_request()
        self._query_control = control = QueryControl()
        self.query_running = True
        self.query_one(QueryPanel).display = True
        self._status("Running SQL… Escape to cancel")
        self._run_query(self.query_one(TextArea).text, request_id, control)

    @work(thread=True, group="query", exit_on_error=False)
    def _run_query(self, sql: str, request_id: int, control: QueryControl) -> None:
        started = perf_counter()
        try:
            with QuerySession(self._path, sql, control, self._query_limits) as session:
                preview = session.preview()
            control.check()
        except (
            duckdb.Error,
            pa.ArrowException,
            OSError,
            ValueError,
            MemoryError,
        ) as exc:
            if not control.cancelled.is_set():
                self.call_from_thread(self._on_query_error, request_id, str(exc))
            return
        except QueryCancelledError:
            return
        self.call_from_thread(
            self._on_query_ok, request_id, preview, perf_counter() - started
        )

    def _on_query_ok(
        self, request_id: int, preview: QueryPreview, elapsed: float
    ) -> None:
        if request_id != self._request_id:
            return
        self.query_running = False
        self._show_table(preview.table)
        suffix = f"preview, {preview.reason}" if preview.truncated else "complete"
        self._status(
            f"SQL · {preview.table.num_rows:,} rows · {suffix} · {elapsed:.2f}s"
        )

    def _on_query_error(self, request_id: int, message: str) -> None:
        if request_id != self._request_id:
            return
        self.query_running = False
        self.query_error = message
        self._status(f"SQL error: {message}")
        if self._get_arrow_table() is None:
            self._show_table(pa.table({}))
        self.query_one(TextArea).focus()

    def action_cancel_query(self) -> None:
        """Interrupt execution and prevent late results from replacing the view."""
        if self.query_running:
            self._new_request()
            self._status("Query cancelled · previous result retained")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route editor buttons to the same actions as keyboard shortcuts."""
        match event.button.id:
            case "run-query":
                self.action_run_query()
            case "cancel-query":
                self.action_cancel_query()
            case "browse-file":
                self.action_browse()
            case _:
                pass

    def on_unmount(self) -> None:
        """Interrupt remaining background computation when the app exits."""
        self._new_request()
        self._page_cancelled.set()

    def _get_arrow_table(self) -> ArrowTable | None:
        """Return the mounted `ArrowTable`, or `None` during the loading phase."""
        try:
            return self.query_one(ArrowTable)
        except NoMatches:
            return None

    def action_toggle_header(self) -> None:
        """Toggle the visibility of the column header row."""
        if (table := self._get_arrow_table()) is not None:
            table.show_header = not table.show_header

    def action_toggle_row_index(self) -> None:
        """Toggle the visibility of the row-index column."""
        if (table := self._get_arrow_table()) is not None:
            table.show_row_index = not table.show_row_index

    def action_toggle_zebra(self) -> None:
        """Toggle zebra striping on data rows."""
        if (table := self._get_arrow_table()) is not None:
            table.zebra_stripes = not table.zebra_stripes

    def action_cycle_cursor_type(self) -> None:
        """Advance the table's cursor type through `_CURSOR_TYPE_CYCLE`."""
        table = self._get_arrow_table()
        if table is None:
            return
        cycle = self._CURSOR_TYPE_CYCLE
        next_index = (cycle.index(table.cursor_type) + 1) % len(cycle)
        table.cursor_type = cycle[next_index]
