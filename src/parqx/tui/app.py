"""Textual application for Parqx."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from enum import Enum, auto
from pathlib import Path
from textwrap import indent
from threading import Event
from time import perf_counter
from typing import Any, ClassVar

import duckdb
import pyarrow as pa
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.reactive import var
from textual.screen import Screen
from textual.widgets import Footer, Label

from parqx.data.parquet import ParquetSource
from parqx.data.result_store import ResultStore
from parqx.data.view import DataPage, ReadCancelledError, TableData, WindowSource
from parqx.query.engine import (
    PreviewLimit,
    QueryCancelledError,
    QueryControl,
    QueryLimits,
    QueryPreview,
    QuerySession,
)
from parqx.tui.widgets import ArrowTable, QueryPanel
from parqx.tui.widgets.arrow_table import CursorType

logger = logging.getLogger(__name__)

_PREVIEW_LIMIT_LABELS = {
    PreviewLimit.ROWS: "row limit",
    PreviewLimit.BYTES: "byte budget",
}


class QueryPhase(Enum):
    """UI query lifecycle, independent of worker events and the displayed result."""

    IDLE = auto()
    RUNNING = auto()
    PREVIEW = auto()
    MATERIALIZING = auto()


class ParqxApp(App[Any]):
    """A Textual App for Parqx."""

    CSS = """
    ArrowTable { height: 1fr; }
    #bottom-area {
        dock: bottom;
        height: auto;
    }
    #bottom-area > Footer { dock: none; }
    #query-status {
        height: auto;
        max-height: 3;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f1", "run_query", "Run", show=False),
        Binding("f2", "cancel_query", "Cancel", show=False),
        Binding("f3", "browse", "Browse", show=False),
        Binding("f4", "load_all", "Load all", show=False),
        Binding("h", "toggle_header", "Header"),
        Binding("i", "toggle_row_index", "Index"),
        Binding("z", "toggle_zebra", "Zebra"),
        Binding("c", "cycle_cursor_type", "Cursor"),
    ]

    query_phase = var(QueryPhase.IDLE, init=False)
    """The current request's phase; a previous result may still be visible."""

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
        self._table = ArrowTable(pa.table({}))
        self._query_panel = QueryPanel(initial_sql or "SELECT * FROM data")
        self._query_status = Label("", id="query-status")
        self._has_result = False  # A successful empty result also counts.
        self._request_id = 0
        self._query_control: QueryControl | None = None
        self.query_error: str | None = None
        self._query_controls: list[QueryControl] = []
        self._result_stores: list[ResultStore] = []
        self._window_source: WindowSource | None = None
        self._page_cancelled = Event()
        self._page_request = 0
        self.load_error: str | None = None
        """Set when the worker thread fails to read the file. The CLI inspects
        this after `run` returns to decide between a clean exit and a non-zero
        exit with an error message."""

    @property
    def query_running(self) -> bool:
        """Whether the current request is executing or loading its full result."""
        return self.query_phase in {QueryPhase.RUNNING, QueryPhase.MATERIALIZING}

    @property
    def can_load_all(self) -> bool:
        """Whether the current request is paused at a resumable preview."""
        return self.query_phase is QueryPhase.PREVIEW

    def compose(self) -> ComposeResult:
        """Reserve one bottom dock for SQL query, status, and footer."""
        yield self._table
        with Vertical(id="bottom-area"):
            yield self._query_panel
            yield self._query_status
            yield Footer(show_command_palette=True)

    def get_system_commands(self, screen: Screen[Any]) -> Iterable[SystemCommand]:
        """Expose the SQL query toggle alongside Textual's built-in commands."""
        # Textual's base signature omits Screen's generic result type.
        yield from super().get_system_commands(screen)  # pyright: ignore[reportUnknownMemberType]
        yield SystemCommand(
            "SQL query",
            "Hide the SQL query panel"
            if self._query_panel.display
            else "Show the SQL query panel",
            self.action_toggle_query,
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Enable applicable query actions outside modal screens."""
        if self.screen.is_modal and action in {
            "run_query",
            "cancel_query",
            "browse",
            "load_all",
        }:
            return False
        if action == "cancel_query":
            return self.query_running or self.can_load_all
        if action == "load_all":
            return self.can_load_all
        return super().check_action(action, parameters)

    def on_mount(self) -> None:
        """Start the initial browse or SQL request after the UI mounts."""
        self._query_panel.display = self._initial_sql is not None
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
            self._publish(self._on_load_error, str(exc), request_id)
            return
        self._publish(self._on_load_ok, source, request_id)

    def _show_table(self, data: TableData, source: WindowSource | None = None) -> None:
        self._page_cancelled.set()
        self._page_request += 1
        previous_source = self._window_source
        self._window_source = source
        if isinstance(previous_source, ResultStore) and previous_source is not source:
            self._close_result(previous_source)
        self._table.replace_data(data)
        self._has_result = True
        # Rendering the uncovered table triggers any needed page reads.
        self._table.loading = False
        self._table.focus()

    @work(thread=True, group="cleanup", exit_on_error=False)
    def _close_result(self, source: ResultStore) -> None:
        source.close()

    def _publish[T, **P](
        self, callback: Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ) -> T | None:
        """Deliver a worker update while tolerating concurrent app shutdown."""
        if not self.is_running:
            return None
        try:
            return self.call_from_thread(callback, *args, **kwargs)
        except RuntimeError:
            if self.is_running:
                raise
            return None

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
        if self._table.data is not event.data or self._window_source is None:
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
                self._publish(self._on_page_error, request, str(exc))
            return
        if not cancelled.is_set():
            self._publish(self._on_page_loaded, data, page, request)

    def _on_page_loaded(self, data: TableData, page: DataPage, request: int) -> None:
        if request == self._page_request and self._table.data is data:
            self._table.accept_page(page)

    def _on_page_error(self, request: int, message: str) -> None:
        if request == self._page_request:
            self._status(f"Read error: {message}")

    def _on_load_error(self, message: str, request_id: int) -> None:
        if request_id != self._request_id:
            return
        self._table.loading = False
        self.load_error = message
        self.exit(return_code=1)

    def _status(self, message: str) -> None:
        self._query_status.update(indent(message, " "))

    def _new_request(self) -> int:
        if self._query_control is not None:
            self._query_control.cancel()
        self._query_control = None
        self._request_id += 1
        self.query_phase = QueryPhase.IDLE
        self.query_error = None
        self._table.loading = False
        return self._request_id

    def action_toggle_query(self) -> None:
        """Toggle SQL query while retaining its text and the displayed result."""
        panel = self._query_panel
        panel.display = not panel.display
        if panel.display:
            panel.editor.focus()
        else:
            self._table.focus()

    def action_browse(self) -> None:
        """Return to the source file's original Arrow values."""
        request_id = self._new_request()
        self._table.loading = not self._has_result
        self._status(f"Loading {self._path.name}…")
        self._load_table(request_id)

    def action_run_query(self) -> None:
        """Submit the SQL query input on a fresh, cancellable worker."""
        request_id = self._new_request()
        self._table.loading = not self._has_result
        self._query_control = control = QueryControl()
        self._query_controls = [
            c for c in self._query_controls if not c.finished.is_set()
        ]
        self._query_controls.append(control)
        self.query_phase = QueryPhase.RUNNING
        self._query_panel.display = True
        self._status("Running SQL… F2 to cancel")
        self._run_query(self._query_panel.editor.text, request_id, control)

    @work(thread=True, group="query", exit_on_error=False)
    def _run_query(self, sql: str, request_id: int, control: QueryControl) -> None:
        control.started.set()
        started = perf_counter()
        store: ResultStore | None = None
        handed_off = False
        try:
            with QuerySession(self._path, sql, control, self._query_limits) as session:
                preview = session.preview()
                control.check()
                self._publish(
                    self._on_query_ok, request_id, preview, perf_counter() - started
                )
                if not preview.truncated:
                    return
                # Pause this execution rather than rerunning a possibly expensive
                # or non-deterministic query when the user asks for all rows.
                control.load_all.wait()
                control.check()
                store = ResultStore(session.schema)
                for preview_batch in preview.table.to_batches(
                    max_chunksize=self._query_limits.batch_rows
                ):
                    control.check()
                    store.append(preview_batch)
                del preview
                control.check()
                handed_off = bool(self._publish(self._on_full_start, request_id, store))
                if not handed_off:
                    return
                last_update = perf_counter()
                while (batch := session.read_batch()) is not None:
                    store.append(batch)
                    if perf_counter() - last_update >= 0.1:
                        self._publish(self._on_full_progress, request_id, store)
                        last_update = perf_counter()
                store.finish()
                self._publish(self._on_full_progress, request_id, store)
        except (
            duckdb.Error,
            pa.ArrowException,
            OSError,
            ValueError,
            MemoryError,
        ) as exc:
            if not control.cancelled.is_set():
                if handed_off and store is not None:
                    self._publish(self._on_full_progress, request_id, store)
                self._publish(self._on_query_error, request_id, str(exc))
            return
        except (QueryCancelledError, ReadCancelledError):
            return
        finally:
            try:
                if store is not None and not handed_off:
                    store.close()
            finally:
                control.finished.set()

    def action_load_all(self) -> None:
        """Continue the current query into a disk-backed, browsable result."""
        if self.can_load_all and self._query_control is not None:
            self.query_phase = QueryPhase.MATERIALIZING
            self._status("Loading full SQL result… F2 to stop")
            self._query_control.load_all.set()

    def _on_full_start(self, request_id: int, store: ResultStore) -> bool:
        if request_id != self._request_id:
            return False
        self._result_stores = [s for s in self._result_stores if not s.closed]
        self._result_stores.append(store)
        cursor = self._table.cursor_coordinate
        self._show_table(TableData(store.schema, store.row_count, None), store)
        self._table.move_cursor(row=cursor.row, column=cursor.column)
        self._on_full_progress(request_id, store)
        return True

    def _on_full_progress(self, request_id: int, store: ResultStore) -> None:
        if request_id != self._request_id or store is not self._window_source:
            return
        self._table.update_row_count(
            store.row_count, store.row_count if store.finished else None
        )
        self.query_phase = (
            QueryPhase.IDLE if store.finished else QueryPhase.MATERIALIZING
        )
        suffix = "complete" if store.finished else "loaded · total unknown · F2 to stop"
        self._status(f"SQL · {store.row_count:,} rows · {suffix}")

    def _on_query_ok(
        self, request_id: int, preview: QueryPreview, elapsed: float
    ) -> None:
        if request_id != self._request_id:
            return
        self.query_phase = QueryPhase.PREVIEW if preview.truncated else QueryPhase.IDLE
        data = TableData.from_table(preview.table)
        if preview.truncated:
            data.total_rows = None
        self._show_table(data)
        suffix = (
            f"preview, {_PREVIEW_LIMIT_LABELS[preview.reason]} · F4 to load all"
            if preview.reason is not None
            else "complete"
        )
        self._status(
            f"SQL · {preview.table.num_rows:,} rows · {suffix} · {elapsed:.2f}s"
        )

    def _on_query_error(self, request_id: int, message: str) -> None:
        if request_id != self._request_id:
            return
        self.query_phase = QueryPhase.IDLE
        self.query_error = message
        self._table.loading = False
        self._status(f"SQL error: {message}")
        self._query_panel.editor.focus()

    def action_cancel_query(self) -> None:
        """Interrupt execution and prevent late results from replacing the view."""
        if self.query_running or self.can_load_all:
            self._new_request()
            self._status("Query cancelled · displayed rows retained")

    async def on_unmount(self) -> None:
        """Interrupt remaining background computation when the app exits."""
        for control in self._query_controls:
            control.cancel()
        self._page_cancelled.set()
        # Include superseded stores whose scheduled cleanup worker may have
        # been cancelled before starting during Textual's shutdown sequence.
        for store in self._result_stores:
            await asyncio.to_thread(store.close)
        for control in self._query_controls:
            if control.started.is_set() and not control.finished.is_set():
                await asyncio.to_thread(control.finished.wait, 5)

    def action_toggle_header(self) -> None:
        """Toggle the visibility of the column header row."""
        self._table.show_header = not self._table.show_header

    def action_toggle_row_index(self) -> None:
        """Toggle the visibility of the row-index column."""
        self._table.show_row_index = not self._table.show_row_index

    def action_toggle_zebra(self) -> None:
        """Toggle zebra striping on data rows."""
        self._table.zebra_stripes = not self._table.zebra_stripes

    def action_cycle_cursor_type(self) -> None:
        """Advance the table's cursor type through `_CURSOR_TYPE_CYCLE`."""
        cycle = self._CURSOR_TYPE_CYCLE
        next_index = (cycle.index(self._table.cursor_type) + 1) % len(cycle)
        self._table.cursor_type = cycle[next_index]
