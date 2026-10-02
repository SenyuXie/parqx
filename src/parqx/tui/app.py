"""Textual application for Parqx."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from textwrap import indent
from time import perf_counter
from typing import Any, ClassVar

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Label

from parqx.query.engine import (
    QueryCancelledError,
    QueryControl,
    QueryLimits,
    QueryPreview,
    QuerySession,
)
from parqx.tui.widgets import ArrowTable, FileLoading, QueryPanel
from parqx.tui.widgets.arrow_table import CursorType

logger = logging.getLogger(__name__)


class ParqxApp(App[Any]):
    """A Textual App for Parqx."""

    CSS = """
    #table-area, ArrowTable { height: 1fr; }
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
        Binding("h", "toggle_header", "Header"),
        Binding("i", "toggle_row_index", "Index"),
        Binding("z", "toggle_zebra", "Zebra"),
        Binding("c", "cycle_cursor_type", "Cursor"),
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
            initial_sql: Optional query to run instead of loading the source table.
            query_limits: Optional preview and execution budget overrides.
        """
        super().__init__()
        self._path = path
        self._initial_sql = initial_sql
        self._query_limits = query_limits or QueryLimits()
        self._table: ArrowTable | None = None
        self._table_area = Container(FileLoading(path), id="table-area")
        self._query_panel = QueryPanel(
            initial_sql if initial_sql is not None else "SELECT * FROM data"
        )
        self._query_status = Label("", id="query-status", markup=False)
        self._request_id = 0
        """Only callbacks for this request may replace results or update status."""
        self._query_control: QueryControl | None = None
        self._query_controls: list[QueryControl] = []
        """Track unfinished queries so shutdown can interrupt and release them."""
        self.query_running = False
        self.query_error: str | None = None
        self.load_error: str | None = None
        """Set when the worker thread fails to read the file. The CLI inspects
        this after `run` returns to decide between a clean exit and a non-zero
        exit with an error message."""

    def compose(self) -> ComposeResult:
        """Yield the table area and a bottom dock for SQL, status, and footer."""
        yield self._table_area
        with Vertical(id="bottom-area"):
            yield self._query_panel
            yield self._query_status
            yield Footer(show_command_palette=True)

    def get_system_commands(self, screen: Screen[Any]) -> Iterable[SystemCommand]:
        """Expose the SQL editor toggle alongside Textual's built-in commands."""
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
        """Enable cancellation only during execution and guard modal screens."""
        if self.screen.is_modal and action in {"run_query", "cancel_query", "browse"}:
            return False
        if action == "cancel_query":
            return self.query_running
        return super().check_action(action, parameters)

    def on_mount(self) -> None:
        """Start the initial browse or SQL request after the UI mounts."""
        self._query_panel.display = self._initial_sql is not None
        if self._initial_sql is not None:
            self.action_run_query()
        else:
            self.action_browse()

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

    @work(thread=True, group="load", exclusive=True, exit_on_error=False)
    def _load_table(self, request_id: int) -> None:
        try:
            table: pa.Table = pq.read_table(self._path)
        except (OSError, pa.ArrowException, MemoryError) as exc:
            logger.exception("Failed to read parquet file: %s", self._path)
            self._publish(self._on_load_error, str(exc), request_id)
            return
        self._publish(self._on_load_ok, table, request_id)

    def _show_table(self, data: pa.Table) -> None:
        if self._table is None:
            self._table = ArrowTable(data)
            self._table_area.query(FileLoading).remove()
            # Mount into the original body even if a command palette is open.
            self._table_area.mount(self._table)
        else:
            self._table.replace_table(data)
        if not self.screen.is_modal:
            self._table.focus()

    def _on_load_ok(self, table: pa.Table, request_id: int) -> None:
        if request_id != self._request_id:
            return
        self._show_table(table)
        self._status(f"{self._path.name} · {table.num_rows:,} rows · original values")

    def _on_load_error(self, message: str, request_id: int) -> None:
        if request_id != self._request_id:
            return
        self.load_error = message
        self.exit(return_code=1)

    def _status(self, message: str) -> None:
        self._query_status.update(indent(message, " "))

    def _new_request(self) -> int:
        if self._query_control is not None:
            self._query_control.cancel()
        self._query_control = None
        self._request_id += 1
        self.query_running = False
        self.query_error = None
        return self._request_id

    def action_toggle_query(self) -> None:
        """Toggle the SQL editor while retaining its text and displayed results."""
        panel = self._query_panel
        panel.display = not panel.display
        if panel.display:
            panel.editor.focus()
        elif self._table is not None:
            self._table.focus()

    def action_browse(self) -> None:
        """Return to the source file's original Arrow values."""
        request_id = self._new_request()
        self._status(f"Loading {self._path.name}…")
        self._load_table(request_id)

    def action_run_query(self) -> None:
        """Submit the complete SQL input on a fresh, cancellable worker."""
        request_id = self._new_request()
        self._query_control = control = QueryControl()
        self._query_controls = [
            item for item in self._query_controls if not item.finished.is_set()
        ]
        self._query_controls.append(control)
        self.query_running = True
        self._query_panel.display = True
        self._status("Running SQL… F2 to cancel")
        self._run_query(self._query_panel.editor.text, request_id, control)

    @work(thread=True, group="query", exit_on_error=False)
    def _run_query(self, sql: str, request_id: int, control: QueryControl) -> None:
        control.started.set()
        started = perf_counter()
        try:
            with QuerySession(self._path, sql, control, self._query_limits) as session:
                preview = session.preview()
            control.check()
            self._publish(
                self._on_query_ok, request_id, preview, perf_counter() - started
            )
        except (
            duckdb.Error,
            pa.ArrowException,
            OSError,
            ValueError,
            MemoryError,
        ) as exc:
            if not control.cancelled.is_set():
                self._publish(self._on_query_error, request_id, str(exc))
        except QueryCancelledError:
            return
        finally:
            control.finished.set()

    def _on_query_ok(
        self, request_id: int, preview: QueryPreview, elapsed: float
    ) -> None:
        if request_id != self._request_id:
            return
        self.query_running = False
        self._show_table(preview.table)
        suffix = (
            f"preview, {preview.reason} · total unknown"
            if preview.truncated
            else "complete"
        )
        self._status(
            f"SQL · {preview.table.num_rows:,} rows · {suffix} · {elapsed:.2f}s"
        )

    def _on_query_error(self, request_id: int, message: str) -> None:
        if request_id != self._request_id:
            return
        self.query_running = False
        self.query_error = message
        if self._table is None:
            self._show_table(pa.table({}))
        self._status(f"SQL error: {message}")
        if not self.screen.is_modal:
            self._query_panel.editor.focus()

    def action_cancel_query(self) -> None:
        """Interrupt execution and prevent late results from replacing the view."""
        if self.query_running:
            self._new_request()
            if self._table is None:
                self._show_table(pa.table({}))
            self._status("Query cancelled · displayed rows retained")

    async def on_unmount(self) -> None:
        """Interrupt unfinished queries and allow worker-owned resources to close."""
        for control in self._query_controls:
            control.cancel()
        for control in self._query_controls:
            if control.started.is_set() and not control.finished.is_set():
                await asyncio.to_thread(control.finished.wait, 5)

    def action_toggle_header(self) -> None:
        """Toggle the visibility of the column header row."""
        if self._table is not None:
            self._table.show_header = not self._table.show_header

    def action_toggle_row_index(self) -> None:
        """Toggle the visibility of the row-index column."""
        if self._table is not None:
            self._table.show_row_index = not self._table.show_row_index

    def action_toggle_zebra(self) -> None:
        """Toggle zebra striping on data rows."""
        if self._table is not None:
            self._table.zebra_stripes = not self._table.zebra_stripes

    def action_cycle_cursor_type(self) -> None:
        """Advance the table's cursor type through `_CURSOR_TYPE_CYCLE`."""
        if self._table is not None:
            cycle = self._CURSOR_TYPE_CYCLE
            next_index = (cycle.index(self._table.cursor_type) + 1) % len(cycle)
            self._table.cursor_type = cycle[next_index]
