"""Textual application for Parqx."""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections import Counter
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from stat import S_ISREG
from threading import Event
from typing import Any, ClassVar

import pyarrow as pa
from textual import on
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.content import Content
from textual.screen import Screen
from textual.widgets import Footer, TabbedContent

from parqx.data.catalog import SourceCatalog, SourceIssue
from parqx.data.parquet import ParquetSource, ReadCancelledError
from parqx.data.view import DataPage, TableData
from parqx.query.engine import QueryLimits
from parqx.tui.screens.query import QueryResult, QueryScreen
from parqx.tui.widgets import ArrowTable, ResultPane
from parqx.tui.widgets.arrow_table import CursorType

logger = logging.getLogger(__name__)


@dataclass
class SourceViewState:
    """Per-view paging state, independent of the retained SQL source catalog."""

    source: ParquetSource | None = None
    page_request: int = 0
    page_cancelled: Event = field(default_factory=Event)


def _open_source(path: Path) -> ParquetSource:
    """Validate and inspect an ordinary file on the source-reading pool."""
    if not S_ISREG(path.stat().st_mode):
        raise OSError(f"Not a regular file: {path}")
    return ParquetSource(path)


class ParqxApp(App[Any]):
    """Inspect Parquet files and keep SQL previews in separate tabs."""

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

    def __init__(
        self, paths: Sequence[Path], query_limits: QueryLimits | None = None
    ) -> None:
        """Initialize ordered sources without reading their Parquet metadata.

        Args:
            paths: Parquet files to load after the UI mounts.
            query_limits: Optional preview and execution budget overrides.
        """
        if not paths:
            raise ValueError("At least one Parquet path is required")
        super().__init__()
        self.catalog = SourceCatalog(paths)
        self._tabs = TabbedContent(id="results")
        name_counts = Counter(
            entry.spec.display_name.casefold() for entry in self.catalog.entries
        )
        self._panes = {
            entry.spec.source_id: ResultPane(
                entry.spec.display_name
                + (
                    f" · {entry.spec.quoted_name}"
                    if name_counts[entry.spec.display_name.casefold()] > 1
                    else ""
                ),
                id=entry.spec.source_id,
                sql_name=entry.spec.quoted_name,
            )
            for entry in self.catalog.entries
        }
        self._source_views = {source_id: SourceViewState() for source_id in self._panes}
        self._tab_lock = asyncio.Lock()
        """Serialize asynchronous mounts/removals and protect the last tab."""
        self._query_number = 0
        self._source_pool = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="parqx-source"
        )
        self._shutting_down = False
        # Textual's registration signature omits the screen's generic result type.
        self.install_screen(  # pyright: ignore[reportUnknownMemberType]
            QueryScreen(self.catalog, query_limits=query_limits), "query"
        )

    @property
    def load_errors(self) -> tuple[SourceIssue, ...]:
        """Return individual source failures for the CLI's exit summary."""
        return tuple(
            entry.issue for entry in self.catalog.entries if entry.issue is not None
        )

    def compose(self) -> ComposeResult:
        """Yield result tabs and the keyboard shortcut footer."""
        yield self._tabs
        yield Footer(show_command_palette=True)

    async def on_mount(self) -> None:
        """Create all source tabs in input order before starting metadata reads."""
        # Dynamically added panes aren't retained by TabbedContent's composition.
        async with self._tab_lock:
            for pane in tuple(self._panes.values()):
                await self._tabs.add_pane(pane)
            for entry in self.catalog.entries:
                self._tabs.get_tab(entry.spec.source_id).tooltip = Content(
                    str(entry.spec.path)
                )
            self._tabs.active = self.catalog.entries[0].spec.source_id
            self._refresh_tab_bindings()
        for entry in self.catalog.entries:
            source_id = entry.spec.source_id
            if entry.issue is not None:
                self._panes[source_id].show_error(str(entry.issue))
            else:
                self.run_worker(
                    self._load_table(source_id),
                    group=f"load:{source_id}",
                    exclusive=True,
                    exit_on_error=False,
                )
        self._exit_if_all_failed()

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
        # TabActivated can arrive after automatic exit has detached the tabs.
        if self._tabs.is_attached:
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

    async def _load_table(self, source_id: str) -> None:
        if self._shutting_down:
            return
        path = self.catalog.get(source_id).spec.path
        try:
            source = await asyncio.get_running_loop().run_in_executor(
                self._source_pool, _open_source, path
            )
        except (OSError, pa.ArrowException, MemoryError) as exc:
            logger.exception("Failed to read parquet file: %s", path)
            self._on_load_error(source_id, str(exc))
            return
        await self._on_load_ok(source_id, source)

    async def _on_load_ok(self, source_id: str, source: ParquetSource) -> None:
        async with self._tab_lock:
            if self._shutting_down:
                return
            self.catalog.mark_ready(source_id)
            self._refresh_query_sources()
            pane = self._panes.get(source_id)
            state = self._source_views.get(source_id)
            if pane is None or state is None:
                return
            state.source = source
            await pane.show_table(
                TableData(source.schema, source.row_count),
                f"{source.row_count:,} rows · original values",
            )
            if self._tabs.active == source_id:
                self._focus_active_table()

    def _on_load_error(self, source_id: str, message: str) -> None:
        if self._shutting_down:
            return
        self.catalog.mark_failed(source_id, message)
        self._refresh_query_sources()
        pane = self._panes.get(source_id)
        if pane is not None:
            issue = self.catalog.get(source_id).issue
            pane.show_error(str(issue))
        self._exit_if_all_failed()

    def _exit_if_all_failed(self) -> None:
        if all(entry.state == "failed" for entry in self.catalog.entries):
            self.exit(return_code=1)

    def _refresh_query_sources(self) -> None:
        query = self.get_screen("query", QueryScreen)  # pyright: ignore[reportUnknownMemberType]
        query.refresh_sources()

    @on(ArrowTable.WindowRequested)
    def _on_window_requested(self, event: ArrowTable.WindowRequested) -> None:
        """Schedule a page only for the source view that requested its window."""
        pane = event.control.parent
        if not isinstance(pane, ResultPane) or pane.id is None:
            return
        source_id = pane.id
        state = self._source_views.get(source_id)
        if (
            self._shutting_down
            or state is None
            or state.source is None
            or self._panes.get(source_id) is not pane
            or pane.table is None
            or pane.table is not event.control
            or pane.table.data is not event.data
        ):
            return
        state.page_cancelled.set()
        state.page_cancelled = cancelled = Event()
        state.page_request += 1
        self.run_worker(
            self._read_page(
                source_id,
                state.source,
                weakref.ref(event.data),
                event.start_row,
                event.stop_row,
                state.page_request,
                cancelled,
            ),
            group=f"page:{source_id}",
            exclusive=True,
            exit_on_error=False,
        )

    async def _read_page(
        self,
        source_id: str,
        source: ParquetSource,
        data: weakref.ReferenceType[TableData],
        start: int,
        stop: int,
        request: int,
        cancelled: Event,
    ) -> None:
        # Native work receives no strong references to the widget or its cache.
        if self._shutting_down or cancelled.is_set():
            return
        try:
            page = await asyncio.get_running_loop().run_in_executor(
                self._source_pool, source.read_window, start, stop, cancelled
            )
        except ReadCancelledError:
            return
        except (OSError, pa.ArrowException, MemoryError) as exc:
            if not cancelled.is_set():
                self._on_page_error(source_id, data, request, str(exc))
            return
        if not cancelled.is_set():
            self._on_page_loaded(source_id, data, request, page)

    def _current_source_pane(
        self, source_id: str, data: weakref.ReferenceType[TableData], request: int
    ) -> ResultPane | None:
        pane = self._panes.get(source_id)
        state = self._source_views.get(source_id)
        if (
            self._shutting_down
            or state is None
            or request != state.page_request
            or state.source is None
            or pane is None
            or pane.table is None
            or pane.table.data is not data()
        ):
            return None
        return pane

    def _on_page_loaded(
        self,
        source_id: str,
        data: weakref.ReferenceType[TableData],
        request: int,
        page: DataPage,
    ) -> None:
        pane = self._current_source_pane(source_id, data, request)
        if pane is not None and pane.table is not None:
            pane.table.accept_page(page)
            pane.update_status(f"{pane.table.row_count:,} rows · original values")

    def _on_page_error(
        self,
        source_id: str,
        data: weakref.ReferenceType[TableData],
        request: int,
        message: str,
    ) -> None:
        pane = self._current_source_pane(source_id, data, request)
        if pane is not None and pane.table is not None:
            pane.table.fail_window()
            pane.update_status(f"Read error: {message}")

    def _cancel_source_read(self, source_id: str) -> None:
        state = self._source_views.pop(source_id, None)
        if state is not None:
            state.page_request += 1
            state.page_cancelled.set()

    def on_unmount(self) -> None:
        """Stop source work and prevent all late UI updates during shutdown."""
        self._shutting_down = True
        for source_id in tuple(self._source_views):
            self._cancel_source_read(source_id)
        self._source_pool.shutdown(wait=False, cancel_futures=True)

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
                self._tabs.get_tab(pane_id).tooltip = Content(details)
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
            if pane_id in self._source_views:
                self.catalog.mark_closed(pane_id)
                self._cancel_source_read(pane_id)
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
