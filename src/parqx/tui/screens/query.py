"""Persistent SQL editor with cancellable preview execution."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import ClassVar, cast

import duckdb
import pyarrow as pa
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Label, LoadingIndicator, TextArea

from parqx.data.catalog import SourceCatalog, SourceIssue, SourceSpec
from parqx.query.engine import (
    QueryCancelledError,
    QueryControl,
    QueryLimits,
    QueryPreview,
    QuerySession,
)


@dataclass(frozen=True)
class QueryResult:
    """A completed SQL preview and the information displayed with its table."""

    sql: str
    preview: QueryPreview
    elapsed: float
    sources: tuple[SourceSpec, ...] = ()
    issues: tuple[SourceIssue, ...] = ()


class QueryScreen(ModalScreen[QueryResult]):
    """Edit and run SQL while preserving the editor between openings."""

    DEFAULT_CSS = """
    QueryScreen {
        align: center middle;
        background: $background 60%;

        & > #query-dialog {
            width: 90%;
            max-width: 100;
            height: 70%;
            max-height: 24;
            padding: 0 1;
            border: solid $primary;
            background: $surface;

            & > TextArea {
                height: 1fr;
                border: none;
            }

            & > #query-loading { height: 1; }
            & > #query-sources { height: 1; }

            & > Label {
                width: 1fr;
                height: auto;
                max-height: 4;
                color: $text-muted;
            }

            & > #query-status.error { color: $error; }
        }
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "run_query", "Run SQL", priority=True),
        Binding("shift+enter", "newline", "New line", priority=True),
        Binding("escape", "close", "Close", priority=True),
    ]

    def __init__(
        self, catalog: SourceCatalog, query_limits: QueryLimits | None = None
    ) -> None:
        """Create the editor without opening a connection or reading the file."""
        super().__init__()
        self._catalog = catalog
        self._query_limits = query_limits or QueryLimits()
        self.editor = TextArea.code_editor("", language="sql", id="sql-query")
        self._status = Label("", id="query-status", markup=False)
        self._sources_label = Label("", id="query-sources", markup=False)
        self._loading = LoadingIndicator(id="query-loading")
        self._loading.display = False
        self._request_id = 0
        """Only the current request may dismiss the modal or update its state."""
        self._query_control: QueryControl | None = None
        self._query_controls: list[QueryControl] = []
        self.running = False
        self.error: str | None = None

    def compose(self) -> ComposeResult:
        """Yield the centered editor, execution status and native shortcut footer."""
        dialog = Vertical(id="query-dialog")
        dialog.border_title = "SQL query"
        with dialog:
            yield self._sources_label
            yield self.editor
            yield self._loading
            yield self._status
            yield Footer(show_command_palette=False)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Keep unavailable editor actions visible but dimmed during execution."""
        if self.running and action in {"run_query", "newline"}:
            return None
        return super().check_action(action, parameters)

    def on_screen_resume(self) -> None:
        """Focus the existing editor without resetting its selection or history."""
        self.error = None
        self._status.update("")
        self._status.remove_class("error")
        self.refresh_sources()
        self.editor.focus()

    def refresh_sources(self) -> None:
        """Refresh source discovery without changing a running query's context."""
        if self.running:
            return
        entries = self._catalog.entries
        self._sources_label.update(
            "Tables: "
            + ", ".join(
                entry.spec.quoted_name
                + (f" ({entry.state})" if entry.state != "ready" else "")
                for entry in entries
            )
        )
        self._sources_label.tooltip = "\n".join(
            f"{entry.spec.quoted_name} → {entry.spec.path}" for entry in entries
        )

    def _set_running(self, running: bool) -> None:
        self.running = running
        self.editor.read_only = running
        self._loading.display = running
        if not running:
            self.refresh_sources()
        self.refresh_bindings()

    def action_newline(self) -> None:
        """Insert a newline through the editor's undoable selection operation."""
        if not self.running:
            start, end = self.editor.selection
            self.editor.replace("\n", start, end, maintain_selection_offset=False)

    def action_run_query(self) -> None:
        """Execute all editor text once, leaving existing result tabs intact."""
        sql = self.editor.text
        if self.running or not sql.strip():
            return
        self._request_id += 1
        self._query_control = control = QueryControl()
        self._query_controls = [
            item for item in self._query_controls if not item.finished.is_set()
        ]
        self._query_controls.append(control)
        self.error = None
        self._status.remove_class("error")
        self._status.update("Running SQL…")
        sources = self._catalog.snapshot()
        unavailable = tuple(
            entry.issue or SourceIssue(entry.spec, "Still loading; try again shortly.")
            for entry in self._catalog.entries
            if entry.state != "ready"
        )
        self.refresh_sources()
        self._set_running(True)
        self._run_query(sources, sql, self._request_id, control, unavailable)

    def _publish[T, **P](
        self, callback: Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ) -> T | None:
        """Deliver a worker update while tolerating concurrent app shutdown."""
        # Textual's app getter omits the generic return type.
        app = cast(App[object], self.app)  # pyright: ignore[reportUnknownMemberType]
        if not app.is_running:
            return None
        try:
            return app.call_from_thread(callback, *args, **kwargs)
        except RuntimeError:
            if app.is_running:
                raise
            return None

    @work(thread=True, group="query", exit_on_error=False)
    def _run_query(
        self,
        sources: tuple[SourceSpec, ...],
        sql: str,
        request_id: int,
        control: QueryControl,
        unavailable: tuple[SourceIssue, ...],
    ) -> None:
        control.started.set()
        started = perf_counter()
        session = QuerySession(sources, sql, control, self._query_limits)
        try:
            with session:
                preview = session.preview()
            control.check()
            self._publish(
                self._on_query_ok,
                request_id,
                QueryResult(
                    sql, preview, perf_counter() - started, sources, session.issues
                ),
            )
        except (
            duckdb.Error,
            pa.ArrowException,
            OSError,
            ValueError,
            MemoryError,
        ) as exc:
            if not control.cancelled.is_set():
                message = str(exc)
                if issues := unavailable + session.issues:
                    message += "\nUnavailable sources:\n" + "\n".join(map(str, issues))
                self._publish(self._on_query_error, request_id, message)
        except QueryCancelledError:
            return
        finally:
            control.finished.set()

    def _is_current(self, request_id: int) -> bool:
        return request_id == self._request_id and self.running and self.is_current

    def _on_query_ok(self, request_id: int, result: QueryResult) -> None:
        if not self._is_current(request_id):
            return
        self._set_running(False)
        self.dismiss(result)

    def _on_query_error(self, request_id: int, message: str) -> None:
        if not self._is_current(request_id):
            return
        self._set_running(False)
        self.error = message
        self._status.update(f"SQL error: {message}")
        self._status.add_class("error")
        self.editor.focus()

    def _cancel_request(self) -> None:
        # Invalidate first so even an already queued worker callback is harmless.
        self._request_id += 1
        if self._query_control is not None:
            self._query_control.cancel()
        self._set_running(False)

    def action_close(self) -> None:
        """Cancel any pending execution and close without returning a result."""
        self._cancel_request()
        self.dismiss()

    def on_screen_suspend(self) -> None:
        """Interrupt execution if another screen unexpectedly covers the editor."""
        if self.running and not self.is_current:
            self._cancel_request()

    async def on_unmount(self) -> None:
        """Interrupt unfinished queries and let their worker-owned resources close."""
        self._request_id += 1
        for control in self._query_controls:
            control.cancel()
        for control in self._query_controls:
            if control.started.is_set() and not control.finished.is_set():
                await asyncio.to_thread(control.finished.wait, 5)
