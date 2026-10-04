"""Textual application for Parqx."""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections import Counter
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial
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
from textual.worker import (
    Worker,
    get_current_worker,  # pyright: ignore[reportUnknownVariableType]
)

from parqx.catalog import SourceCatalog, SourceIssue
from parqx.data.parquet import ParquetSource, ReadCancelledError
from parqx.data.view import DataPage, TableData
from parqx.query.engine import QueryLimits
from parqx.tui.screens.query import QueryResult, QueryScreen
from parqx.tui.widgets import ArrowTable, ResultPane
from parqx.tui.widgets.arrow_table import CursorType

logger = logging.getLogger(__name__)


def _open_source(path: Path) -> ParquetSource:
    """Validate and inspect an ordinary file on the source-reading pool."""
    if not S_ISREG(path.stat().st_mode):
        raise OSError(f"Not a regular file: {path}")
    return ParquetSource(path)


def _query_result_details(result: QueryResult) -> str:
    """Format preview status, SQL and source warnings for a result tab."""
    preview = result.preview
    status = (
        f"preview, {preview.reason} · total unknown"
        if preview.truncated
        else "complete"
    )
    if result.issues:
        status += f" · warning: {len(result.issues)} sources unavailable"
    details = (
        f"{preview.table.num_rows:,} rows · {status} · {result.elapsed:.2f}s"
        f"\n\n{result.sql}"
    )
    if result.issues:
        details += "\n\nUnavailable sources:\n" + "\n".join(map(str, result.issues))
    return details


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
        self._panes: dict[str, ResultPane] = {}
        for entry in self.catalog.entries:
            source = entry.spec
            title = source.display_name
            if name_counts[title.casefold()] > 1:
                title += f" · {source.quoted_name}"
            self._panes[source.source_id] = ResultPane(title, id=source.source_id)
        # Only open browsing views retain source metadata; SQL uses the catalog.
        self._source_views: dict[str, ParquetSource | None] = dict.fromkeys(self._panes)
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
                    f"{entry.spec.path}"
                )
            self._tabs.active = self.catalog.entries[0].spec.source_id
            self._refresh_tab_bindings()
        for entry in self.catalog.entries:
            source_id = entry.spec.source_id
            if entry.issue is not None:
                self._panes[source_id].show_error(str(entry.issue))
            else:
                _worker: Worker[None] = self.run_worker(
                    partial(self._load_table, source_id),
                    description="Load source metadata",
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
            pane = self._panes.get(source_id)
            if pane is None or source_id not in self._source_views:
                return
            self._source_views[source_id] = source
            await pane.show_table(TableData(source.schema, source.row_count))
            if self._tabs.active == source_id:
                self._focus_active_table()

    def _on_load_error(self, source_id: str, message: str) -> None:
        if self._shutting_down:
            return
        self.catalog.mark_failed(source_id, message)
        pane = self._panes.get(source_id)
        if pane is not None:
            issue = self.catalog.get(source_id).issue
            pane.show_error(str(issue))
        self._exit_if_all_failed()

    def _exit_if_all_failed(self) -> None:
        if all(entry.state == "failed" for entry in self.catalog.entries):
            self.exit(return_code=1)

    @on(ArrowTable.WindowRequested)
    def _on_window_requested(self, event: ArrowTable.WindowRequested) -> None:
        """Schedule a page only for the source view that requested its window."""
        pane = event.control.parent
        if not isinstance(pane, ResultPane) or pane.id is None:
            return
        source_id = pane.id
        source = self._source_views.get(source_id)
        if (
            self._shutting_down
            or source is None
            or self._panes.get(source_id) is not pane
            or pane.table is None
            or pane.table is not event.control
            or pane.table.data is not event.data
        ):
            return
        # Superseded workers may never start, so create their coroutine lazily.
        _worker: Worker[None] = self.run_worker(
            partial(
                self._read_page,
                source_id,
                source,
                weakref.ref(event.data),
                event.start_row,
                event.stop_row,
            ),
            description="Read source page",
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
    ) -> None:
        # One worker signal cancels both awaiting the result and native batches.
        cancelled = get_current_worker().cancelled_event
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
                self._on_page_error(source_id, data, cancelled, str(exc))
            return
        if not cancelled.is_set():
            self._on_page_loaded(source_id, data, cancelled, page)

    def _current_source_table(
        self, source_id: str, data: weakref.ReferenceType[TableData], cancelled: Event
    ) -> ArrowTable | None:
        pane = self._panes.get(source_id)
        if (
            self._shutting_down
            or cancelled.is_set()
            or self._source_views.get(source_id) is None
            or pane is None
            or pane.table is None
            or pane.table.data is not data()
        ):
            return None
        return pane.table

    def _on_page_loaded(
        self,
        source_id: str,
        data: weakref.ReferenceType[TableData],
        cancelled: Event,
        page: DataPage,
    ) -> None:
        table = self._current_source_table(source_id, data, cancelled)
        if table is not None:
            table.accept_page(page)

    def _on_page_error(
        self,
        source_id: str,
        data: weakref.ReferenceType[TableData],
        cancelled: Event,
        message: str,
    ) -> None:
        table = self._current_source_table(source_id, data, cancelled)
        if table is not None:
            table.fail_window()
            self.notify(
                str(SourceIssue(self.catalog.get(source_id).spec, message)),
                title="Read error",
                severity="error",
                markup=False,
            )

    def _cancel_source_read(self, source_id: str) -> None:
        # Preserve metadata loading for the retained SQL source catalog.
        self.workers.cancel_group(self, f"page:{source_id}")  # pyright: ignore[reportUnknownMemberType]
        self._source_views.pop(source_id, None)

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
                pane = ResultPane(
                    f"Query {self._query_number}",
                    id=pane_id,
                    table=result.preview.table,
                )
                self._panes[pane_id] = pane
                await self._tabs.add_pane(pane)
                self._tabs.get_tab(pane_id).tooltip = Content(
                    _query_result_details(result)
                )
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
                self._cancel_source_read(pane_id)
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
