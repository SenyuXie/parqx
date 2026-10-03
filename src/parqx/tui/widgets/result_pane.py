"""An independent table and status line within a result tab."""

import pyarrow as pa
from textual.app import ComposeResult
from textual.content import Content
from textual.widgets import Label, TabPane

from parqx.data.view import TableData
from parqx.tui.widgets.arrow_table import ArrowTable


class ResultPane(TabPane):
    """Keep a result's data and table view state together until the tab closes."""

    DEFAULT_CSS = """
    ResultPane {
        height: 1fr;
        & > ArrowTable { height: 1fr; }
        & > .result-status {
            height: auto;
            max-height: 3;
            padding: 0 1;
            color: $text-muted;
        }
    }
    """

    def __init__(
        self,
        title: str,
        *,
        id: str,
        table: pa.Table | None = None,
        status: str = "",
        sql_name: str | None = None,
    ) -> None:
        """Initialize a populated result or a placeholder for the source read."""
        super().__init__(Content(title), id=id)
        self.table = ArrowTable(table) if table is not None else None
        self.loading = table is None
        self._sql_name = sql_name
        self._status = Label("", classes="result-status", markup=False)
        self.update_status(status)

    def compose(self) -> ComposeResult:
        """Yield the available table and its status beneath the loading cover."""
        if self.table is not None:
            yield self.table
        yield self._status

    async def show_table(self, table: pa.Table | TableData, status: str) -> None:
        """Mount source metadata before letting rendering request missing windows."""
        self.table = ArrowTable(table)
        await self.mount(self.table, before=self._status)
        self.update_status(status)
        self.loading = False

    def update_status(self, status: str) -> None:
        """Update this result's status without moving focus or changing tabs."""
        if self._sql_name is not None:
            status = f"SQL: {self._sql_name}" + (f" · {status}" if status else "")
        self._status.update(status)
        self._status.display = bool(status)

    def show_error(self, message: str) -> None:
        """Replace a source's loading cover with its individual failure."""
        self.loading = False
        self.update_status(f"Open error: {message}")
        self._status.tooltip = Content(message)

    def on_unmount(self) -> None:
        """Release Arrow buffers even if Textual briefly retains the closed widget."""
        if self.table is not None:
            self.table.replace_table(pa.table({}))
            self.table = None
