"""Persistent SQL editor with threaded preview execution."""

from __future__ import annotations

from typing import ClassVar

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Footer, LoadingIndicator, TextArea
from textual.worker import Worker, WorkerState

from parqx.catalog import SourceCatalog, SourceIssue, SourceSpec
from parqx.config import ParqxConfig
from parqx.query.engine import QueryExecutionError, QueryResult, execute_query


class QueryScreen(ModalScreen[QueryResult]):
    """Edit and run SQL while preserving the editor between openings."""

    DEFAULT_CSS = """
    QueryScreen {
        align: center top;
        background: $background 60%;

        & > #query-dialog {
            width: 80%;
            max-width: 90;
            height: 4;
            padding: 0 1;
            border: solid $primary;
            background: $surface;

            & > TextArea {
                height: 1fr;
                min-height: 1;
                border: none;
                scrollbar-size-horizontal: 0;
                overflow-y: hidden;
            }

            & > #query-loading { height: 1; }
        }

        &.compact > #query-dialog {
            width: 100%;
        }
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "run_query", "Run SQL", priority=True),
        Binding("shift+enter", "newline", "New line", priority=True),
        Binding("escape", "close", "Close", priority=True),
    ]

    def __init__(self, catalog: SourceCatalog, *, config: ParqxConfig) -> None:
        """Create the editor without opening a connection or reading the file."""
        super().__init__()
        self._catalog = catalog
        self._config = config
        self._dialog = Vertical(id="query-dialog")
        self._dialog.border_title = "SQL query"
        self.editor = TextArea.code_editor("", language="sql", id="sql-query")
        self._loading = LoadingIndicator(id="query-loading")
        self._loading.display = False
        self._footer = Footer(show_command_palette=False)
        self._current_worker: Worker[QueryResult] | None = None
        """Only this worker may finish the dialog's current request."""

    def compose(self) -> ComposeResult:
        """Yield the expanding editor, loading indicator and shortcut footer."""
        with self._dialog:
            yield self.editor
            yield self._loading
            yield self._footer

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Keep unavailable editor actions visible but dimmed during execution."""
        if self.running and action in {"run_query", "newline", "close"}:
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
        # Two border rows, the footer and an optional loading row.
        chrome_height = 3 + int(self.running)
        line_count = self.editor.document.line_count
        height = min(line_count + chrome_height, 24, max(4, self.size.height))
        # TextArea checks for overflow before the larger dialog is laid out.
        # Only allow a scrollbar when the lines exceed the target editor height.
        self.editor.styles.overflow_y = (
            "auto" if line_count > height - chrome_height else "hidden"
        )
        self._dialog.styles.height = height
        # Place the input row one sixth down, allowing for the top border.
        # Keep the starting row steady as the editor grows, moving up only
        # when necessary to keep the footer inside a short terminal.
        top = min(max(0, self.size.height // 6 - 1), max(0, self.size.height - height))
        self._dialog.styles.margin = (top, 0, 0, 0)
        self.editor.call_after_refresh(self.editor.scroll_cursor_visible)

    @property
    def running(self) -> bool:
        """Whether a request still owns the query dialog."""
        return self._current_worker is not None

    def _set_current_worker(self, worker: Worker[QueryResult] | None) -> None:
        self._current_worker = worker
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
        sources = self._catalog.snapshot()
        unavailable = tuple(
            entry.issue or SourceIssue(entry.spec, "Still loading; try again shortly.")
            for entry in self._catalog.entries
            if entry.state != "ready"
        )
        self._set_current_worker(self._run_query(sources, sql, unavailable))

    @work(thread=True, exit_on_error=False, group="query")
    def _run_query(
        self,
        sources: tuple[SourceSpec, ...],
        sql: str,
        unavailable: tuple[SourceIssue, ...],
    ) -> QueryResult:
        return execute_query(sources, sql, config=self._config, unavailable=unavailable)

    @on(Worker.StateChanged)
    def _on_query_state_changed(self, event: Worker.StateChanged) -> None:
        worker = self._current_worker
        # Textual omits the result type on StateChanged.worker.
        if event.worker is not worker or worker is None or not self.is_mounted:  # pyright: ignore[reportUnknownMemberType]
            return
        if event.state not in {WorkerState.SUCCESS, WorkerState.ERROR}:
            return
        self._set_current_worker(None)
        if not self.is_active:
            return
        if event.state == WorkerState.SUCCESS:
            self.dismiss(worker.result)
        else:
            error = worker.error
            message = str(error) or type(error).__name__
            if isinstance(error, QueryExecutionError) and error.issues:
                message += "\nUnavailable sources:\n" + "\n".join(
                    map(str, error.issues)
                )
            self.notify(
                message, title="SQL error", severity="error", timeout=4, markup=False
            )
            self.editor.focus()

    def action_close(self) -> None:
        """Close the editor only while it is idle."""
        if not self.running:
            self.dismiss(None)

    def on_unmount(self) -> None:
        """Forget delivery ownership while native work finishes independently."""
        self._current_worker = None
