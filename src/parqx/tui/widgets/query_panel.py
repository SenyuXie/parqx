"""SQL editor and query controls."""

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static, TextArea


class QueryPanel(Vertical):
    """Edit a query over the current Parquet file's data view."""

    DEFAULT_CSS = """
    QueryPanel {
        height: 10;
        & > Static { height: 1; color: $text-muted; }
        & > TextArea { height: 1fr; }
        & > Horizontal { height: 3; }
        Button { min-width: 12; margin-right: 1; }
    }
    """

    def __init__(self, sql: str = "SELECT * FROM data") -> None:
        """Initialize the editor without executing SQL."""
        super().__init__()
        self._sql = sql

    def compose(self) -> ComposeResult:
        """Yield an editor and explicit execution controls."""
        yield Static("Current file: data · F5 / Ctrl+Enter to run · Escape to cancel")
        yield TextArea(
            self._sql, id="sql-editor", soft_wrap=False, show_line_numbers=True
        )
        with Horizontal():
            yield Button("Run", id="run-query", variant="primary")
            yield Button("Cancel", id="cancel-query")
            yield Button("Browse file", id="browse-file")
