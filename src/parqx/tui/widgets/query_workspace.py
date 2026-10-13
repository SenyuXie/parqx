"""Persistent SQL authoring and its latest successful result."""

import pyarrow as pa
from textual.app import ComposeResult
from textual.content import Content
from textual.widgets import Static, TabPane, TextArea

from parqx.query.engine import QueryResult
from parqx.tui.widgets.arrow_table import ArrowTable


class QueryWorkspace(TabPane):
    """Keep one independent SQL editor mounted across tab changes."""

    DEFAULT_CSS = """
    QueryWorkspace {
        height: 1fr;
        & > ArrowTable { height: 1fr; }
        & > .query-status { height: auto; max-height: 4; }
        & > TextArea {
            height: 10;
            max-height: 50%;
            min-height: 3;
            border: solid $primary;
        }
    }
    """

    def __init__(self, title: str, *, id: str) -> None:
        """Create a blank SQL draft without starting an execution."""
        super().__init__(Content(title), id=id)
        self.editor = TextArea.code_editor("", language="sql", classes="sql-draft")
        self.editor.border_title = "SQL · Ctrl+Enter to run"
        self.current_result: QueryResult | None = None
        self.error: str | None = None
        self.table: ArrowTable | None = None
        self.status = Static(
            "Write SQL, then press Ctrl+Enter", classes="query-status", markup=False
        )

    def compose(self) -> ComposeResult:
        """Yield the persistent SQL editor."""
        yield self.editor
        yield self.status

    def on_resize(self) -> None:
        """Keep the insertion point visible after a terminal layout change."""
        self.editor.call_after_refresh(self.editor.scroll_cursor_visible)

    def on_text_area_changed(self) -> None:
        """Scroll after the editor has recalculated its multiline extent."""
        self.editor.call_after_refresh(self.editor.scroll_cursor_visible)

    @property
    def running(self) -> bool:
        """Whether this draft is reserved by the application's execution slot."""
        return self.editor.read_only

    def start_execution(self) -> None:
        """Keep the current result visible while locking the submitted draft."""
        self.editor.read_only = True
        self.error = None
        self.status.update("Running…")

    def finish_execution(self) -> None:
        """Restore editing after the application applies completion."""
        self.editor.read_only = False

    async def show_result(self, result: QueryResult) -> None:
        """Replace data and schema without moving focus or creating another tab."""
        if self.table is None:
            self.table = ArrowTable(result.table)
            await self.mount(self.table)
        else:
            self.table.replace_data(result.table)
        self.current_result = result
        self.error = None
        self.status.update(f"{result.table.num_rows:,} rows · {result.elapsed:.2f}s")

    def show_error(self, message: str) -> None:
        """Retain the last successful result alongside the failed attempt."""
        self.error = message
        self.status.update(f"SQL error: {message}")

    def on_unmount(self) -> None:
        """Release result buffers even if a detached widget is still referenced."""
        self.current_result = None
        if self.table is not None:
            self.table.replace_data(pa.table({}))
