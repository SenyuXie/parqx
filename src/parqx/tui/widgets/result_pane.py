"""An independent table or loading error within a result tab."""

import pyarrow as pa
from textual.app import ComposeResult
from textual.content import Content
from textual.widgets import Static, TabPane

from parqx.data.view import TableData
from parqx.tui.widgets.arrow_table import ArrowTable


class ResultPane(TabPane):
    """Keep a result's data and table view state together until the tab closes."""

    DEFAULT_CSS = """
    ResultPane {
        height: 1fr;
        & > ArrowTable { height: 1fr; }
        & > .source-error {
            width: 1fr;
            height: 1fr;
            padding: 1 2;
            content-align: center middle;
            color: $error;
        }
    }
    """

    def __init__(self, title: str, *, id: str, table: pa.Table | None = None) -> None:
        """Initialize a populated result or a placeholder for the source read."""
        super().__init__(Content(title), id=id)
        self.table = ArrowTable(table) if table is not None else None
        self.loading = table is None
        self._error = Static("", classes="source-error", markup=False)
        self._error.display = False

    def compose(self) -> ComposeResult:
        """Yield the table and an error placeholder beneath the loading cover."""
        if self.table is not None:
            yield self.table
        yield self._error

    async def show_table(self, table: pa.Table | TableData) -> None:
        """Mount source metadata before letting rendering request missing windows."""
        self.table = ArrowTable(table)
        self._error.display = False
        await self.mount(self.table)
        self.loading = False

    def show_error(self, message: str) -> None:
        """Replace a source's loading cover with its individual failure."""
        self.loading = False
        self._error.update(f"Open error: {message}")
        self._error.tooltip = Content(message)
        self._error.display = True

    def on_unmount(self) -> None:
        """Release Arrow buffers even if Textual briefly retains the closed widget."""
        if self.table is not None:
            self.table.replace_table(pa.table({}))
            self.table = None
