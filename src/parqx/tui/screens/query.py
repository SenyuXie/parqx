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
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Footer, LoadingIndicator, TextArea

from parqx.catalog import SourceCatalog, SourceIssue, SourceSpec
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
        align: center top;
        background: $background 60%;

        & > #query-dialog {
            width: 90%;
            max-width: 100;
            height: 4;
            padding: 0 1;
            border: solid $primary;
            background: $surface;

            & > TextArea {
                height: 1fr;
                min-height: 1;
                border: none;
                scrollbar-size-horizontal: 0;
            }

            & > #query-loading { height: 1; }
        }

        &.compact > #query-dialog {
            width: 100%;
        }
    }
    """

    _SHUTDOWN_TIMEOUT: ClassVar[float] = 5.0

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
        self._dialog = Vertical(id="query-dialog")
        self._dialog.border_title = "SQL query"
        self.editor = TextArea.code_editor("", language="sql", id="sql-query")
        self._loading = LoadingIndicator(id="query-loading")
        self._loading.display = False
        self._footer = Footer(show_command_palette=False)
        self._current_control: QueryControl | None = None
        """Only this request may dismiss the modal or update its state."""
        self._query_controls: list[QueryControl] = []
        """Track queued and unfinished requests, including cancelled older ones."""

    def compose(self) -> ComposeResult:
        """Yield the expanding editor, loading indicator and shortcut footer."""
        with self._dialog:
            yield self.editor
            yield self._loading
            yield self._footer

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Keep unavailable editor actions visible but dimmed during execution."""
        if self.running and action in {"run_query", "newline"}:
            return None
        return super().check_action(action, parameters)

    def on_screen_resume(self) -> None:
        """Focus the existing editor without resetting its selection or history."""
        self._resize_dialog()
        self.editor.focus()

    def on_resize(self, event: Resize) -> None:
        """Reserve room for editing and shortcuts on a small terminal."""
        self.set_class(event.size.height < 18 or event.size.width < 60, "compact")
        self._footer.compact = event.size.width < 60
        self._resize_dialog()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Resize after typing, paste, deletion, undo or loading saved SQL."""
        self._resize_dialog()

    def _resize_dialog(self) -> None:
        # Two border rows and the shortcut footer sit outside the editor.
        height = min(
            self.editor.document.line_count + 3 + int(self.running),
            24,
            max(4, self.size.height),
        )
        self._dialog.styles.height = height
        # Keep the starting row steady as the editor grows, moving up only
        # when necessary to keep the footer inside a short terminal.
        top = min(
            max(0, (self.size.height - 4) // 3), max(0, self.size.height - height)
        )
        self._dialog.styles.margin = (top, 0, 0, 0)
        self.editor.call_after_refresh(self.editor.scroll_cursor_visible)

    @property
    def running(self) -> bool:
        """Whether a request still owns the query dialog."""
        return self._current_control is not None

    def _set_current_control(self, control: QueryControl | None) -> None:
        self._current_control = control
        running = self.running
        self.editor.read_only = running
        self._loading.display = running
        self._resize_dialog()
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
        control = QueryControl()
        self._query_controls = [
            item for item in self._query_controls if not item.finished.is_set()
        ]
        self._query_controls.append(control)
        sources = self._catalog.snapshot()
        unavailable = tuple(
            entry.issue or SourceIssue(entry.spec, "Still loading; try again shortly.")
            for entry in self._catalog.entries
            if entry.state != "ready"
        )
        self._set_current_control(control)
        self._run_query(sources, sql, control, unavailable)

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
                control,
                QueryResult(
                    sql,
                    preview,
                    perf_counter() - started,
                    sources,
                    unavailable + session.issues,
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
                if sources:
                    message += "\nQuery sources:\n" + "\n".join(
                        f"{source.quoted_name} → {source.path}" for source in sources
                    )
                self._publish(self._on_query_error, control, message)
        except QueryCancelledError:
            return
        finally:
            control.finished.set()

    def _is_current(self, control: QueryControl) -> bool:
        return control is self._current_control and self.is_active

    def _on_query_ok(self, control: QueryControl, result: QueryResult) -> None:
        if not self._is_current(control):
            return
        self._set_current_control(None)
        self.dismiss(result)

    def _on_query_error(self, control: QueryControl, message: str) -> None:
        if not self._is_current(control):
            return
        self._set_current_control(None)
        self.notify(
            message, title="SQL error", severity="error", timeout=4, markup=False
        )
        self.editor.focus()

    def _cancel_request(self) -> None:
        # Invalidate first so even an already queued worker callback is harmless.
        control = self._current_control
        self._set_current_control(None)
        if control is not None:
            control.cancel()

    def action_close(self) -> None:
        """Cancel any pending execution and close without returning a result."""
        self._cancel_request()
        self.dismiss()

    def on_screen_suspend(self) -> None:
        """Interrupt execution if another screen unexpectedly covers the editor."""
        if self.running and not self.is_active:
            self._cancel_request()

    async def on_unmount(self) -> None:
        """Interrupt unfinished queries and let their worker-owned resources close."""
        self._current_control = None
        controls = tuple(self._query_controls)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._SHUTDOWN_TIMEOUT
        for control in controls:
            control.cancel()
        # The default executor also runs SQL; do not queue cleanup waits on it.
        # This bounds cooperative waiting, not the lifetime of native threads.
        while any(
            control.started.is_set() and not control.finished.is_set()
            for control in controls
        ):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.01, remaining))
