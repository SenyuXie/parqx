"""Persistent SQL authoring and its latest successful result."""

from typing import ClassVar

import pyarrow as pa
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TabPane, TextArea

from parqx.query.engine import QueryResult
from parqx.tui.widgets.arrow_table import ArrowTable


def _result_summary(result: QueryResult) -> str:
    completeness = "preview · total unknown" if result.truncation_reason else "complete"
    summary = f"{result.table.num_rows:,} rows · {result.elapsed:.2f}s · {completeness}"
    if result.issues:
        summary += f" · {len(result.issues)} source warning(s)"
    return summary


def query_result_details(result: QueryResult) -> str:
    """Describe the successful execution without retaining any earlier result."""
    details = _result_summary(result)
    if result.truncation_reason:
        details += f"\nPreview limited by {result.truncation_reason}; total unknown."
    details += f"\n\nSuccessfully executed SQL:\n{result.sql}"
    if result.issues:
        details += "\n\nUnavailable sources:\n" + "\n".join(map(str, result.issues))
    return details


class QueryDetails(ModalScreen[None]):
    """Readable, selectable text for the current result and latest error."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss", "Close", priority=True)
    ]
    DEFAULT_CSS = """
    QueryDetails {
        align: center middle;
        & > Vertical {
            width: 90%;
            max-width: 100;
            height: 85%;
            border: solid $primary;
            background: $surface;
            padding: 0 1;
        }
        TextArea { height: 1fr; }
        Horizontal { height: 3; align-horizontal: right; }
        Button { margin-left: 1; }
    }
    """

    def __init__(self, details: str, error: str | None) -> None:
        """Capture only display text for this inspection."""
        super().__init__()
        self.details = details
        self.error = error

    def compose(self) -> ComposeResult:
        """Provide native text selection and explicit clipboard actions."""
        with Vertical():
            yield Static("Query details")
            yield TextArea(self.details, read_only=True, soft_wrap=True)
            with Horizontal():
                if self.error is not None:
                    yield Button("Copy error", id="copy-error")
                yield Button("Copy details", id="copy-details")
                yield Button("Close", id="close-details")

    @on(Button.Pressed)
    def handle_button(self, event: Button.Pressed) -> None:
        """Copy the requested text or restore the workspace's focus."""
        event.stop()
        if event.button.id == "close-details":
            self.dismiss()
        elif event.button.id == "copy-error" and self.error is not None:
            self.app.copy_to_clipboard(self.error)  # pyright: ignore[reportUnknownMemberType]
        elif event.button.id == "copy-details":
            self.app.copy_to_clipboard(self.details)  # pyright: ignore[reportUnknownMemberType]


class QueryWorkspace(TabPane):
    """Keep one independent SQL editor mounted across tab changes."""

    DEFAULT_CSS = """
    QueryWorkspace {
        height: 1fr;
        & > ArrowTable { height: 1fr; }
        .query-feedback { height: auto; max-height: 4; }
        .query-status { width: 1fr; height: auto; max-height: 4; }
        .editor-controls { height: 1; }
        .editor-controls Static { width: 1fr; }
        .editor-controls Button {
            min-width: 3;
            height: 1;
            margin-left: 1;
        }
        .toggle-editor { width: 12; }
        .result-details { width: 16; min-width: 16; height: 1; }
        & > TextArea {
            height: 10;
            min-height: 4;
            border: solid $primary;
        }
    }
    """

    def __init__(self, title: str, *, id: str) -> None:
        """Create a blank SQL draft without starting an execution."""
        super().__init__(Content(title), id=id)
        self.editor = TextArea.code_editor("", language="sql", classes="sql-draft")
        self.editor.border_title = "SQL · Ctrl+Enter to run"
        self._editor_height = 10
        self.current_result: QueryResult | None = None
        self.error: str | None = None
        self.table: ArrowTable | None = None
        self.status = Static(
            "No result yet · Ctrl+Enter to run", classes="query-status", markup=False
        )

    def compose(self) -> ComposeResult:
        """Yield the persistent SQL editor."""
        with Horizontal(classes="editor-controls"):
            yield Static("SQL editor")
            yield Button(
                "-", classes="shrink-editor", compact=True, tooltip="Shrink SQL editor"
            )
            yield Button(
                "+", classes="grow-editor", compact=True, tooltip="Grow SQL editor"
            )
            yield Button("Hide SQL", classes="toggle-editor", compact=True)
        yield self.editor
        with Horizontal(classes="query-feedback"):
            yield self.status
            yield Button(
                "Result details", classes="result-details", disabled=True, compact=True
            )

    def on_mount(self) -> None:
        """Let native maximization temporarily override the saved editor size."""
        self.watch(self.screen, "maximized", self._apply_editor_layout, init=False)

    def on_resize(self) -> None:
        """Adapt the saved allocation while keeping result controls reachable."""
        self._apply_editor_layout()
        self.editor.call_after_refresh(self.editor.scroll_cursor_visible)

    def _apply_editor_layout(self) -> None:
        if not self.size.height:
            return
        # Reserve one control row, up to four feedback rows and four result rows.
        maximum = max(4, self.size.height - 9)
        height = min(self._editor_height, maximum)
        self.editor.styles.height = (
            "1fr" if self.screen.maximized is self.editor else height
        )
        self.query_one(".shrink-editor", Button).disabled = (
            not self.editor.display or height <= 4
        )
        self.query_one(".grow-editor", Button).disabled = (
            not self.editor.display or height >= maximum
        )
        self.query_one(".toggle-editor", Button).label = (
            "Hide SQL" if self.editor.display else "Show SQL"
        )

    @on(Button.Pressed, ".shrink-editor, .grow-editor")
    def resize_editor(self, event: Button.Pressed) -> None:
        """Adjust only this workspace's preferred editor allocation."""
        event.stop()
        change = -2 if event.button.has_class("shrink-editor") else 2
        self._editor_height = max(4, self.editor.region.height + change)
        self._apply_editor_layout()
        self.editor.call_after_refresh(self.editor.scroll_cursor_visible)

    @on(Button.Pressed, ".toggle-editor")
    def toggle_editor(self, event: Button.Pressed) -> None:
        """Collapse the SQL draft or restore its saved allocation."""
        event.stop()
        self.editor.display = not self.editor.display
        self._apply_editor_layout()
        if self.editor.display:
            self.editor.focus()

    def on_text_area_changed(self) -> None:
        """Scroll after the editor has recalculated its multiline extent."""
        self.editor.call_after_refresh(self.editor.scroll_cursor_visible)
        self._update_status()

    @property
    def running(self) -> bool:
        """Whether this draft is reserved by the application's execution slot."""
        return self.editor.read_only

    def start_execution(self) -> None:
        """Keep the current result visible while locking the submitted draft."""
        self.editor.read_only = True
        self.error = None
        self._update_status()

    def finish_execution(self) -> None:
        """Restore editing after the application applies completion."""
        self.editor.read_only = False
        self._update_status()

    async def show_result(self, result: QueryResult) -> None:
        """Replace data and schema without moving focus or creating another tab."""
        if self.table is None:
            self.table = ArrowTable(result.table)
            await self.mount(self.table)
        else:
            self.table.replace_data(result.table)
        self.current_result = result
        self.error = None
        self._update_status()

    def show_error(self, message: str) -> None:
        """Retain the last successful result alongside the failed attempt."""
        self.error = message
        self._update_status()

    def _update_status(self) -> None:
        result = self.current_result
        if result is None:
            status = "No result yet"
        else:
            status = _result_summary(result)
            if self.running or self.error is not None:
                status += " · previous successful result"
            elif self.editor.text != result.sql:
                status += " · previously executed SQL"
        if self.running:
            status = f"Running… · {status}"
        elif self.error is not None:
            status = f"SQL error · {status}\n{self.error}"
        elif result is None:
            status += " · Ctrl+Enter to run"
        self.status.update(status)
        if self.is_mounted:
            self.query_one(".result-details", Button).disabled = (
                result is None and self.error is None
            )

    @on(Button.Pressed, ".result-details")
    def show_details(self, event: Button.Pressed) -> None:
        """Inspect full execution metadata and copy a persistent error."""
        event.stop()
        details = (
            query_result_details(self.current_result)
            if self.current_result is not None
            else "No successful result yet."
        )
        if self.error is not None:
            details = f"Latest attempt failed:\n{self.error}\n\n{details}"
        self.app.push_screen(QueryDetails(details, self.error))  # pyright: ignore[reportUnknownMemberType]

    def on_unmount(self) -> None:
        """Release result buffers even if a detached widget is still referenced."""
        self.current_result = None
        if self.table is not None:
            self.table.replace_data(pa.table({}))
