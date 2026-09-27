"""SQL query input with native keyboard editing."""

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import TextArea


class QueryPanel(Vertical):
    """Edit a query over the current Parquet file's data view."""

    DEFAULT_CSS = """
    QueryPanel {
        height: 7;
        & > TextArea { height: 1fr; }
    }
    """

    def __init__(self, sql: str = "SELECT * FROM data") -> None:
        """Initialize the SQL query input without executing it."""
        super().__init__()
        self.editor = TextArea.code_editor(sql, language="sql", id="sql-query")
        self.editor.border_title = "SQL query"
        self.editor.border_subtitle = (
            "F1 Run · F2 Cancel · F3 Browse · F4 All · Esc Back"
        )

    def compose(self) -> ComposeResult:
        """Yield the SQL query input with shortcut hints in its border."""
        yield self.editor
