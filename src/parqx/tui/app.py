"""Textual application for Parqx."""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Callable, Iterable
from pathlib import Path
from threading import Event
from typing import Any, ClassVar

import pyarrow as pa
from textual import on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import Footer, TabbedContent

from parqx.data.catalog import SourceCatalog
from parqx.data.parquet import ParquetSource, ReadCancelledError
from parqx.data.view import DataPage, TableData
from parqx.query.engine import QueryLimits
from parqx.tui.screens.query import QueryResult, QueryScreen
from parqx.tui.widgets import ArrowTable, ResultPane
from parqx.tui.widgets.arrow_table import CursorType

logger = logging.getLogger(__name__)


class ParqxApp(App[Any]):
    """Inspect a Parquet file and keep SQL previews in separate tabs."""

    CSS = """
    #results, #results > ContentSwitcher { height: 1fr; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+w", "close_tab", "Close tab"),
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

    def __init__(self, path: Path, query_limits: QueryLimits | None = None) -> None:
        """Initialize the app without reading the source file.

        Args:
            path: Parquet file to load after the UI mounts.
            query_limits: Optional preview and execution budget overrides.
        """
        super().__init__()
        self._path = path
        self.catalog = SourceCatalog([path])
        self._tabs = TabbedContent(id="results")
        self._panes = {"source": ResultPane(path.name, id="source")}
        self._tab_lock = asyncio.Lock()
        """Serialize asynchronous mounts/removals and protect the last tab."""
        self._query_number = 0
        self._load_request_id = 0
        self._source: ParquetSource | None = None
        self._page_request = 0
        self._page_cancelled = Event()
        # Textual's registration signature omits the screen's generic result type.
        self.install_screen(  # pyright: ignore[reportUnknownMemberType]
            QueryScreen(self.catalog, query_limits=query_limits), "query"
        )
        self.load_error: str | None = None
        """The CLI reports a source read failure after the app exits."""

    def compose(self) -> ComposeResult:
        """Yield result tabs and the keyboard shortcut footer."""
        yield self._tabs
        yield Footer(show_command_palette=True)

    async def on_mount(self) -> None:
        """Open the source tab before starting the background file read."""
        # Add dynamically so TabbedContent's composition doesn't retain a closed
        # source pane and all of its Arrow buffers for the rest of the app's life.
        await self._tabs.add_pane(self._panes["source"])
        self._tabs.active = "source"
        self._refresh_tab_bindings()
        self._load_table(self._load_request_id)

    def get_system_commands(self, screen: Screen[Any]) -> Iterable[SystemCommand]:
        """Expose the SQL dialog alongside Textual's built-in commands."""
        # Textual's base signature omits Screen's generic result type.
        yield from super().get_system_commands(screen)  # pyright: ignore[reportUnknownMemberType]
        yield SystemCommand(
            "SQL query", "Run SQL against loaded Parquet files", self.action_open_query
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Protect modal input and keep the last tab's close hint visible."""
        if action == "command_palette" and isinstance(self.screen, QueryScreen):
            return False
        if (
            action
            in {
                "close_tab",
                "open_query",
                "toggle_header",
                "toggle_row_index",
                "toggle_zebra",
                "cycle_cursor_type",
            }
            and self.screen.is_modal
        ):
            return False
        if action == "close_tab":
            return True if len(self._panes) > 1 else None
        return super().check_action(action, parameters)

    def _refresh_tab_bindings(self) -> None:
        # Update the main footer even while another screen is on top of it.
        self._tabs.screen.refresh_bindings()

    def _active_table(self) -> ArrowTable | None:
        pane = self._panes.get(self._tabs.active)
        return pane.table if pane is not None else None

    def _focus_active_table(self) -> None:
        if not self.screen.is_modal and (table := self._active_table()) is not None:
            table.focus()

    @on(TabbedContent.TabActivated, "#results")
    def _on_tab_activated(self) -> None:
        self._refresh_tab_bindings()

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
            source = ParquetSource(self._path)
        except (OSError, pa.ArrowException, MemoryError) as exc:
            logger.exception("Failed to read parquet file: %s", self._path)
            self._publish(self._on_load_error, str(exc), request_id)
            return
        self._publish(self._on_load_ok, source, request_id)

    async def _on_load_ok(self, source: ParquetSource, request_id: int) -> None:
        async with self._tab_lock:
            if request_id != self._load_request_id:
                return
            self.catalog.mark_ready("source-1")
            self._refresh_query_sources()
            pane = self._panes.get("source")
            if pane is None:
                return
            self._source = source
            await pane.show_table(
                TableData(source.schema, source.row_count),
                f"{source.row_count:,} rows · original values",
            )
            if self._tabs.active == "source":
                self._focus_active_table()

    def _on_load_error(self, message: str, request_id: int) -> None:
        if request_id == self._load_request_id:
            self.catalog.mark_failed("source-1", message)
            self._refresh_query_sources()
            if "source" in self._panes:
                self.load_error = message
                self.exit(return_code=1)

    def _refresh_query_sources(self) -> None:
        query = self.get_screen("query", QueryScreen)  # pyright: ignore[reportUnknownMemberType]
        query.refresh_sources()

    @on(ArrowTable.WindowRequested)
    def _on_window_requested(self, event: ArrowTable.WindowRequested) -> None:
        """Read source windows on workers without changing the active result tab."""
        pane = self._panes.get("source")
        if (
            self._source is None
            or pane is None
            or pane.table is None
            or pane.table is not event.control
            or pane.table.data is not event.data
        ):
            return
        self._page_cancelled.set()
        self._page_cancelled = cancelled = Event()
        self._page_request += 1
        self._read_page(
            self._source,
            weakref.ref(event.data),
            event.start_row,
            event.stop_row,
            self._page_request,
            cancelled,
        )

    @work(thread=True, group="page", exclusive=True, exit_on_error=False)
    def _read_page(
        self,
        source: ParquetSource,
        data: weakref.ReferenceType[TableData],
        start: int,
        stop: int,
        request: int,
        cancelled: Event,
    ) -> None:
        # A closed pane can release its cache while this bounded read finishes.
        try:
            page = source.read_window(start, stop, cancelled)
        except ReadCancelledError:
            return
        except (OSError, pa.ArrowException, MemoryError) as exc:
            if not cancelled.is_set():
                self._publish(self._on_page_error, data, request, str(exc))
            return
        if not cancelled.is_set():
            self._publish(self._on_page_loaded, data, request, page)

    def _current_source_pane(
        self, data: weakref.ReferenceType[TableData], request: int
    ) -> ResultPane | None:
        pane = self._panes.get("source")
        if (
            request != self._page_request
            or self._source is None
            or pane is None
            or pane.table is None
            or pane.table.data is not data()
        ):
            return None
        return pane

    def _on_page_loaded(
        self, data: weakref.ReferenceType[TableData], request: int, page: DataPage
    ) -> None:
        pane = self._current_source_pane(data, request)
        if pane is not None and pane.table is not None:
            pane.table.accept_page(page)
            pane.update_status(f"{pane.table.row_count:,} rows · original values")

    def _on_page_error(
        self, data: weakref.ReferenceType[TableData], request: int, message: str
    ) -> None:
        pane = self._current_source_pane(data, request)
        if pane is not None and pane.table is not None:
            pane.table.fail_window()
            pane.update_status(f"Read error: {message}")

    def _cancel_source_read(self, *, cancel_load: bool = True) -> None:
        if cancel_load:
            self._load_request_id += 1
        self._page_request += 1
        self._page_cancelled.set()
        self._source = None

    def on_unmount(self) -> None:
        """Invalidate source callbacks and cancel any in-flight window read."""
        self._cancel_source_read()

    def action_open_query(self) -> None:
        """Open the reusable SQL editor without replacing the current tab."""
        if not self.screen.is_modal:
            self.push_screen("query", self._on_query_result)

    async def _on_query_result(self, result: QueryResult | None) -> None:
        if result is not None:
            async with self._tab_lock:
                self._query_number += 1
                pane_id = f"query-{self._query_number}"
                preview = result.preview
                suffix = (
                    f"preview, {preview.reason} · total unknown"
                    if preview.truncated
                    else "complete"
                )
                if result.issues:
                    suffix += f" · warning: {len(result.issues)} sources unavailable"
                pane = ResultPane(
                    f"Query {self._query_number}",
                    id=pane_id,
                    table=preview.table,
                    status=f"{preview.table.num_rows:,} rows · {suffix} · {result.elapsed:.2f}s",
                )
                self._panes[pane_id] = pane
                await self._tabs.add_pane(pane)
                context = "\n".join(
                    f"{source.quoted_name} → {source.path}" for source in result.sources
                )
                details = result.sql + (f"\n\nSources:\n{context}" if context else "")
                if result.issues:
                    details += "\n\nUnavailable sources:\n" + "\n".join(
                        map(str, result.issues)
                    )
                self._tabs.get_tab(pane_id).tooltip = details
                self._tabs.active = pane_id
                self._refresh_tab_bindings()
        self.call_after_refresh(self._focus_active_table)

    async def action_close_tab(self) -> None:
        """Close the active page while always retaining at least one tab."""
        async with self._tab_lock:
            if self.screen.is_modal or len(self._panes) <= 1:
                return
            pane_id = self._tabs.active
            if pane_id not in self._panes:
                return
            if pane_id == "source":
                self.catalog.mark_closed("source-1")
                self._cancel_source_read(cancel_load=False)
                self._refresh_query_sources()
            await self._tabs.remove_pane(pane_id)
            del self._panes[pane_id]
            self._refresh_tab_bindings()
            self.call_after_refresh(self._focus_active_table)

    def action_toggle_header(self) -> None:
        """Toggle the active table's column header row."""
        if (table := self._active_table()) is not None:
            table.show_header = not table.show_header

    def action_toggle_row_index(self) -> None:
        """Toggle the active table's row-index column."""
        if (table := self._active_table()) is not None:
            table.show_row_index = not table.show_row_index

    def action_toggle_zebra(self) -> None:
        """Toggle zebra striping on the active table."""
        if (table := self._active_table()) is not None:
            table.zebra_stripes = not table.zebra_stripes

    def action_cycle_cursor_type(self) -> None:
        """Advance the active table's cursor type."""
        if (table := self._active_table()) is not None:
            cycle = self._CURSOR_TYPE_CYCLE
            table.cursor_type = cycle[(cycle.index(table.cursor_type) + 1) % len(cycle)]
