"""Textual application for Parqx."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from stat import S_ISREG
from typing import Any, ClassVar

from textual import on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.content import Content
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Footer, TabbedContent, TabPane, Tree
from textual.worker import Worker, WorkerState

from parqx.catalog import SourceCatalog, SourceIssue, SourceSpec
from parqx.config import ParqxConfig
from parqx.data.parquet import ParquetSource
from parqx.query.engine import QueryExecutionError, QueryResult, execute_query
from parqx.tui.widgets import ArrowTable, TablePane
from parqx.tui.widgets.arrow_table import CursorType
from parqx.tui.widgets.query_workspace import QueryWorkspace
from parqx.tui.widgets.sources_tree import SourcesTree

logger = logging.getLogger(__name__)


def _open_source(path: Path, config: ParqxConfig) -> ParquetSource:
    """Validate and inspect an ordinary file on the source-reading pool."""
    if not S_ISREG(path.stat().st_mode):
        raise OSError(f"Not a regular file: {path}")
    return ParquetSource(
        path,
        page_rows=config.page_rows,
        page_bytes=config.page_bytes,
        memory_limit=config.memory_limit,
        threads=config.threads,
    )


def _query_result_details(result: QueryResult) -> str:
    """Format result status, executed SQL and source warnings for its workspace."""
    status = (
        f"preview, {result.truncation_reason} · total unknown"
        if result.truncation_reason is not None
        else "complete"
    )
    if result.issues:
        status += f" · warning: {len(result.issues)} sources unavailable"
    details = (
        f"{result.table.num_rows:,} rows · {status} · {result.elapsed:.2f}s"
        f"\n\n{result.sql}"
    )
    if result.issues:
        details += "\n\nUnavailable sources:\n" + "\n".join(map(str, result.issues))
    return details


class ParqxApp(App[Any]):
    """Inspect Parquet files alongside persistent SQL query workspaces."""

    CSS = """
    #main { height: 1fr; }
    #sources { width: 32; max-width: 40%; height: 1fr; border-right: solid $primary; }
    #results { width: 1fr; }
    #results, #results > ContentSwitcher { height: 1fr; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+w", "close_tab", "Close tab", priority=True),
        Binding("ctrl+enter", "run_query", "Run SQL", priority=True),
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
        self, paths: Sequence[Path], *, config: ParqxConfig | None = None
    ) -> None:
        """Initialize ordered sources without reading their Parquet metadata.

        Args:
            paths: Parquet files to load after the UI mounts.
            config: Optional preview and execution budget overrides.
        """
        if not paths:
            raise ValueError("At least one Parquet path is required")
        super().__init__()
        self.config = config or ParqxConfig()
        self.catalog = SourceCatalog(paths)
        self._sources = SourcesTree(self.catalog.entries)
        self._sources.display = False
        self._tabs = TabbedContent(id="results")
        self._tab_lock = asyncio.Lock()
        """Serialize asynchronous mounts/removals and protect the last tab."""
        self._query_number = 0
        self._execution: tuple[Worker[list[QueryResult]], QueryWorkspace] | None = None
        self._source_pool = ThreadPoolExecutor(
            max_workers=self.config.source_workers, thread_name_prefix="parqx-source"
        )
        self._loading_sources: set[str] = set()
        self._shutting_down = False

    @property
    def load_errors(self) -> tuple[SourceIssue, ...]:
        """Return individual source failures for the CLI's exit summary."""
        return tuple(
            entry.issue for entry in self.catalog.entries if entry.issue is not None
        )

    def compose(self) -> ComposeResult:
        """Yield source and query tabs, the source sidebar, and the shortcut footer."""
        with Horizontal(id="main"):
            yield self._sources
            yield self._tabs
        yield Footer(show_command_palette=True)

    async def on_mount(self) -> None:
        """Create all source tabs in input order before starting metadata reads."""
        # Dynamically added panes aren't retained by TabbedContent's composition.
        async with self._tab_lock:
            for entry in self.catalog.entries:
                await self._add_source_pane(entry.spec.source_id)
            self._tabs.active = self.catalog.entries[0].spec.source_id
            self._refresh_tab_bindings()
        for entry in self.catalog.entries:
            source_id = entry.spec.source_id
            if entry.issue is None:
                self._start_source_load(source_id)
        self._exit_if_all_failed()

    async def _add_source_pane(self, source_id: str) -> TablePane:
        entry = self.catalog.get(source_id)
        source = entry.spec
        title = source.display_name
        if (
            sum(
                item.spec.display_name.casefold() == title.casefold()
                for item in self.catalog.entries
            )
            > 1
        ):
            title += f" · {source.quoted_name}"
        pane = TablePane(
            title, source, id=source_id, executor=self._source_pool, config=self.config
        )
        await self._tabs.add_pane(pane)
        self._tabs.get_tab(source_id).tooltip = Content(str(source.path))
        # A pending read can fail while the new pane is mounting.
        if (issue := self.catalog.get(source_id).issue) is not None:
            pane.show_error(issue)
        return pane

    @on(Tree.NodeSelected, "#sources")
    async def _on_source_selected(self, event: Tree.NodeSelected[str]) -> None:
        source_id = event.node.data
        if source_id is None or self.screen.is_modal:
            return
        event.stop()
        async with self._tab_lock:
            if self._source_pane(source_id) is None:
                await self._add_source_pane(source_id)
                if self.catalog.get(source_id).state == "ready":
                    self._start_source_load(source_id)
            self._tabs.active = source_id
            self._refresh_tab_bindings()

    def get_system_commands(self, screen: Screen[Any]) -> Iterable[SystemCommand]:
        """Expose query workspace creation alongside native commands."""
        # Textual's base signature omits Screen's generic result type.
        yield from super().get_system_commands(screen)  # pyright: ignore[reportUnknownMemberType]
        yield SystemCommand(
            "Query", "Open a blank SQL workspace", self.action_open_query
        )
        yield SystemCommand(
            "Toggle Sources", "Show or hide source schemas", self.action_toggle_sources
        )

    def action_toggle_sources(self) -> None:
        """Show or hide the session's shared source structure."""
        if not self.screen.is_modal:
            had_focus = self._sources.has_focus
            self._sources.display = not self._sources.display
            if had_focus and not self._sources.display:
                self.screen.focus_next()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Protect modal input and keep the last tab's close hint visible."""
        if (
            action
            in {
                "close_tab",
                "open_query",
                "run_query",
                "toggle_header",
                "toggle_row_index",
                "toggle_zebra",
                "cycle_cursor_type",
            }
            and self.screen.is_modal
        ):
            return False
        if action in {
            "toggle_header",
            "toggle_row_index",
            "toggle_zebra",
            "cycle_cursor_type",
        }:
            return isinstance(self.focused, ArrowTable)
        if action == "run_query":
            return isinstance(self._active_pane(), QueryWorkspace)
        if action == "close_tab":
            return True if self._tabs.is_mounted and self._tabs.tab_count > 1 else None
        return super().check_action(action, parameters)

    def _refresh_tab_bindings(self) -> None:
        # Update the main footer even while another screen is on top of it.
        # TabActivated can arrive after automatic exit has detached the tabs.
        if self._tabs.is_attached:
            self._tabs.screen.refresh_bindings()

    def _active_pane(self) -> TabPane | None:
        if not self._tabs.is_mounted:
            return None
        try:
            return self._tabs.active_pane
        except NoMatches:
            # Native pane removal and active-tab updates occur in separate turns.
            return None

    def _active_table(self) -> ArrowTable | None:
        if not self._tabs.is_mounted:
            return None
        pane = self._active_pane()
        return pane.table if isinstance(pane, (TablePane, QueryWorkspace)) else None

    def _source_pane(self, source_id: str) -> TablePane | None:
        if not self._tabs.is_mounted:
            return None
        try:
            pane = self._tabs.get_pane(source_id)
        except NoMatches:
            return None
        return pane if isinstance(pane, TablePane) else None

    def _focus_active_table(self) -> None:
        if not self.screen.is_modal and (table := self._active_table()) is not None:
            table.focus()

    @on(TabbedContent.TabActivated, "#results")
    def _on_tab_activated(self) -> None:
        self._refresh_tab_bindings()

    def _start_source_load(self, source_id: str) -> None:
        if self._shutting_down or source_id in self._loading_sources:
            return
        # A view can close and reopen while its app-owned metadata read runs.
        self._loading_sources.add(source_id)
        _worker: Worker[None] = self.run_worker(
            partial(self._load_table, source_id),
            description="Load source metadata",
            group=f"load:{source_id}",
        )

    async def _load_table(self, source_id: str) -> None:
        path = self.catalog.get(source_id).spec.path
        try:
            if self._shutting_down:
                return
            try:
                source = await asyncio.get_running_loop().run_in_executor(
                    self._source_pool, _open_source, path, self.config
                )
            except Exception as exc:
                logger.exception("Failed to read parquet file: %s", path)
                self._on_load_error(source_id, str(exc) or type(exc).__name__)
                return
            await self._on_load_ok(source_id, source)
        finally:
            self._loading_sources.discard(source_id)

    async def _on_load_ok(self, source_id: str, source: ParquetSource) -> None:
        async with self._tab_lock:
            if self._shutting_down:
                return
            self.catalog.mark_ready(source_id, source.schema)
            self._sources.update_source(self.catalog.get(source_id))
            pane = self._source_pane(source_id)
            if pane is None:
                return
            await pane.set_source(source)
            if self._tabs.active == source_id and not (
                self._sources.display and self._sources.has_focus
            ):
                self._focus_active_table()

    def _on_load_error(self, source_id: str, message: str) -> None:
        if self._shutting_down:
            return
        self.catalog.mark_failed(source_id, message)
        self._sources.update_source(self.catalog.get(source_id))
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
        if self._execution is not None:
            results = self._execution[0].result
            if results is not None:
                results.clear()
        self._execution = None
        self._source_pool.shutdown(wait=False, cancel_futures=True)

    async def action_open_query(self) -> None:
        """Create and activate a new blank SQL workspace."""
        if self.screen.is_modal:
            return
        async with self._tab_lock:
            self._query_number += 1
            pane_id = f"query-{self._query_number}"
            pane = QueryWorkspace(f"Query {self._query_number}", id=pane_id)
            await self._tabs.add_pane(pane)
            self._tabs.active = pane_id
            self._refresh_tab_bindings()
            pane.editor.focus()

    def action_run_query(self) -> None:
        """Submit the entire active workspace draft to the single execution slot."""
        if self.screen.is_modal or self._shutting_down:
            return
        pane = self._active_pane()
        if not isinstance(pane, QueryWorkspace) or not pane.editor.text.strip():
            return
        if self._execution is not None:
            self.notify("A query is already running", title="Query busy", markup=False)
            return
        sql = pane.editor.text
        sources = self.catalog.snapshot()
        unavailable = tuple(
            entry.issue or SourceIssue(entry.spec, "Still loading; try again shortly.")
            for entry in self.catalog.entries
            if entry.state != "ready"
        )
        pane.start_execution()
        self._execution = (self._run_query(sources, sql, unavailable), pane)

    @work(thread=True, exit_on_error=False, group="query")
    def _run_query(
        self,
        sources: tuple[SourceSpec, ...],
        sql: str,
        unavailable: tuple[SourceIssue, ...],
    ) -> list[QueryResult]:
        # Textual retains the last worker in executor-thread context. Consume this
        # one-shot result on delivery so that context cannot retain Arrow buffers.
        result = execute_query(
            sources, sql, config=self.config, unavailable=unavailable
        )
        return [] if self._shutting_down else [result]

    @on(Worker.StateChanged)
    async def _on_query_state_changed(self, event: Worker.StateChanged) -> None:
        execution = self._execution
        if execution is None:
            return
        worker, pane = execution
        if event.worker is not worker:  # pyright: ignore[reportUnknownMemberType]
            return
        if event.state not in {WorkerState.SUCCESS, WorkerState.ERROR}:
            return
        try:
            results = worker.result
            if self._shutting_down or not pane.is_attached:
                if results is not None:
                    results.clear()
                return
            if event.state == WorkerState.SUCCESS:
                if results:
                    result = results.pop()
                    await pane.show_result(result)
                    if pane.is_attached and not self._shutting_down:
                        self._tabs.get_tab(pane).tooltip = Content(
                            _query_result_details(result)
                        )
            else:
                error = worker.error
                message = str(error) or type(error).__name__
                if isinstance(error, QueryExecutionError) and error.issues:
                    message += "\nUnavailable sources:\n" + "\n".join(
                        map(str, error.issues)
                    )
                pane.show_error(message)
                self.notify(
                    message,
                    title="SQL error",
                    severity="error",
                    timeout=4,
                    markup=False,
                )
        finally:
            # Keep ownership until data and presentation have both been applied.
            if self._execution is execution:
                self._execution = None
            if pane.is_attached and not self._shutting_down:
                pane.finish_execution()

    async def action_close_tab(self) -> None:
        """Close the active page while always retaining at least one tab."""
        async with self._tab_lock:
            if (
                self.screen.is_modal
                or not self._tabs.is_mounted
                or self._tabs.tab_count <= 1
            ):
                return
            pane = self._active_pane()
            if isinstance(pane, QueryWorkspace) and pane.running:
                self.notify(
                    "The running query cannot be closed",
                    title="Query busy",
                    markup=False,
                )
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
