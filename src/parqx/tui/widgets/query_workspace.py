"""Persistent SQL authoring and its latest successful result."""

import pyarrow as pa
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.content import Content
from textual.widgets import Button, TabPane, TextArea

from parqx.query.engine import QueryResult
from parqx.tui.widgets.arrow_table import ArrowTable


class QueryWorkspace(TabPane):
    """Keep one independent SQL editor mounted across tab changes."""

    DEFAULT_CSS = """
    QueryWorkspace {
        height: 1fr;
        & > ArrowTable {
            height: 1fr;
        }
        .query-actions {
            height: 1;
            padding: 0 1;
            align-horizontal: right;
        }
        .run-query { width: 13; min-width: 13; height: 1; }
        & > TextArea {
            height: 10;
            min-height: 4;
        }
    }
    """

    def __init__(self, title: str, *, id: str) -> None:
        """Create a blank SQL draft without starting an execution."""
        super().__init__(Content(title), id=id)
        self.editor = TextArea.code_editor("", language="sql", classes="sql-draft")
        self.editor.border_title = "SQL Editor"
        self.run_button = Button(
            "Run Query",
            classes="run-query",
            variant="primary",
            compact=True,
            disabled=True,
            tooltip="Ctrl+Enter",
        )
        self.current_result: QueryResult | None = None
        self.table: ArrowTable | None = None

    def compose(self) -> ComposeResult:
        """Yield the persistent SQL editor."""
        yield self.editor
        with Horizontal(classes="query-actions"):
            yield self.run_button

    def on_mount(self) -> None:
        """Let native maximization temporarily fill the screen with the editor."""
        self.watch(self.screen, "maximized", self._apply_editor_layout, init=False)

    def on_resize(self) -> None:
        """Adapt the editor allocation while leaving room for the button and results."""
        self._apply_editor_layout()
        self.editor.call_after_refresh(self.editor.scroll_cursor_visible)

    def _apply_editor_layout(self) -> None:
        if not self.size.height:
            return
        # Reserve one button row and four result rows.
        height = min(10, max(4, self.size.height - 5))
        self.editor.styles.height = (
            "1fr" if self.screen.maximized is self.editor else height
        )

    def on_text_area_changed(self) -> None:
        """Scroll after the editor has recalculated its multiline extent."""
        self.editor.call_after_refresh(self.editor.scroll_cursor_visible)
        self._update_run_button()

    @property
    def running(self) -> bool:
        """Whether this draft is reserved by the application's execution slot."""
        return self.editor.read_only

    def start_execution(self) -> None:
        """Keep the current result visible while locking the submitted draft."""
        self.editor.read_only = True
        self._update_run_button()

    def finish_execution(self) -> None:
        """Restore editing after the application applies completion."""
        self.editor.read_only = False
        self._update_run_button()

    async def show_result(self, result: QueryResult) -> None:
        """Replace data and schema without moving focus or creating another tab."""
        if self.table is None:
            self.table = ArrowTable(result.table)
            await self.mount(self.table)
        else:
            self.table.replace_data(result.table)
        self.current_result = result

    def _update_run_button(self) -> None:
        self.run_button.disabled = self.running or not self.editor.text.strip()

    def on_unmount(self) -> None:
        """Release result buffers even if a detached widget is still referenced."""
        self.current_result = None
        if self.table is not None:
            self.table.replace_data(pa.table({}))
