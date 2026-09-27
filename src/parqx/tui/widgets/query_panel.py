"""SQL editor and query controls."""

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, TextArea


class QueryPanel(Vertical):
    """Edit a query over the current Parquet file's data view."""

    DEFAULT_CSS = """
    QueryPanel {
        height: 10;
        & > TextArea { height: 1fr; }
        & > Horizontal { height: 3; }
        Button { min-width: 12; margin-right: 1; }
    }
    """

    def __init__(self, sql: str = "SELECT * FROM data") -> None:
        """Initialize the editor without executing SQL."""
        super().__init__()
        self.editor = TextArea.code_editor(sql, language="sql", id="sql-editor")
        self.load_all = Button(
            "Load all", id="load-all", action="app.load_all", disabled=True
        )

    def compose(self) -> ComposeResult:
        """Yield an editor and explicit execution controls."""
        yield self.editor
        with Horizontal():
            yield Button(
                "Run", id="run-query", variant="primary", action="app.run_query"
            )
            yield Button("Cancel", id="cancel-query", action="app.cancel_query")
            yield Button("Browse file", id="browse-file", action="app.browse")
            yield self.load_all
