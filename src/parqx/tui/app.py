"""Textual application for Parqx."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from stat import S_ISREG
from typing import Any, ClassVar

from textual import on
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.content import Content
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Footer, TabbedContent
from textual.worker import Worker

from parqx.catalog import SourceCatalog, SourceIssue
from parqx.data.parquet import ParquetSource
from parqx.query.engine import QueryLimits, QueryResult
from parqx.tui.screens.query import QueryScreen
from parqx.tui.widgets import ArrowTable, ResultPane, SourcePane, TablePane
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
        name_counts = Counter(
            entry.spec.display_name.casefold() for entry in self.catalog.entries
        )
        async with self._tab_lock:
            for entry in self.catalog.entries:
                source = entry.spec
                title = source.display_name
                if name_counts[title.casefold()] > 1:
                    title += f" · {source.quoted_name}"
                pane = SourcePane(title, source, self._source_pool)
                await self._tabs.add_pane(pane)
                self._tabs.get_tab(source.source_id).tooltip = Content(str(source.path))
                if entry.issue is not None:
                    pane.show_error(entry.issue)
            self._tabs.active = self.catalog.entries[0].spec.source_id
            self._refresh_tab_bindings()
        for entry in self.catalog.entries:
            source_id = entry.spec.source_id
            if entry.issue is None:
                _worker: Worker[None] = self.run_worker(
                    partial(self._load_table, source_id),
                    description="Load source metadata",
                    group=f"load:{source_id}",
                    exclusive=True,
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
            return True if self._tabs.is_mounted and self._tabs.tab_count > 1 else None
        return super().check_action(action, parameters)

    def _refresh_tab_bindings(self) -> None:
        # Update the main footer even while another screen is on top of it.
        # TabActivated can arrive after automatic exit has detached the tabs.
        if self._tabs.is_attached:
            self._tabs.screen.refresh_bindings()

    def _active_table(self) -> ArrowTable | None:
        if not self._tabs.is_mounted:
            return None
        pane = self._tabs.active_pane
        return pane.table if isinstance(pane, TablePane) else None

    def _source_pane(self, source_id: str) -> SourcePane | None:
        if not self._tabs.is_mounted:
            return None
        try:
            pane = self._tabs.get_pane(source_id)
        except NoMatches:
            return None
        return pane if isinstance(pane, SourcePane) else None

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
        except Exception as exc:
            logger.exception("Failed to read parquet file: %s", path)
            self._on_load_error(source_id, str(exc) or type(exc).__name__)
            return
        await self._on_load_ok(source_id, source)

    async def _on_load_ok(self, source_id: str, source: ParquetSource) -> None:
        async with self._tab_lock:
            if self._shutting_down:
                return
            self.catalog.mark_ready(source_id)
            pane = self._source_pane(source_id)
            if pane is None:
                return
            await pane.set_source(source)
            if self._tabs.active == source_id:
                self._focus_active_table()

    def _on_load_error(self, source_id: str, message: str) -> None:
        if self._shutting_down:
            return
        self.catalog.mark_failed(source_id, message)
        pane = self._source_pane(source_id)
        issue = self.catalog.get(source_id).issue
        if pane is not None and issue is not None:
            pane.show_error(issue)
        self._exit_if_all_failed()

    def _exit_if_all_failed(self) -> None:
        if all(entry.state == "failed" for entry in self.catalog.entries):
            self.exit(return_code=1)

    def on_unmount(self) -> None:
        """Stop source work and prevent all late UI updates during shutdown."""
        self._shutting_down = True
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
            if (
                self.screen.is_modal
                or not self._tabs.is_mounted
                or self._tabs.tab_count <= 1
            ):
                return
            pane_id = self._tabs.active
            if not pane_id:
                return
            await self._tabs.remove_pane(pane_id)
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
