"""An independent table and status line within a result tab."""

from pathlib import Path

import pyarrow as pa
from textual.app import ComposeResult
from textual.content import Content
from textual.widgets import Label, TabPane

from parqx.tui.widgets.arrow_table import ArrowTable
from parqx.tui.widgets.file_loading import FileLoading


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
        loading_path: Path | None = None,
    ) -> None:
        """Initialize a populated result or a placeholder for the source read."""
        super().__init__(Content(title), id=id)
        self.table = ArrowTable(table) if table is not None else None
        self._loading_path = loading_path
        self._status = Label(status, classes="result-status", markup=False)
        self._status.display = bool(status)

    def compose(self) -> ComposeResult:
        """Yield the table or loading indicator, followed by its status."""
        if self.table is not None:
            yield self.table
        elif self._loading_path is not None:
            yield FileLoading(self._loading_path)
        yield self._status

    async def show_table(self, table: pa.Table, status: str) -> None:
        """Replace a loading placeholder with the completed source table."""
        await self.query(FileLoading).remove()
        self.table = ArrowTable(table)
        await self.mount(self.table, before=self._status)
        self._status.update(status)
        self._status.display = True

    def on_unmount(self) -> None:
        """Release Arrow buffers even if Textual briefly retains the closed widget."""
        if self.table is not None:
            self.table.replace_table(pa.table({}))
            self.table = None
