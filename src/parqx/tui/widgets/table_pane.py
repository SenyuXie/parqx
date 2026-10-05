"""Shared table ownership for source and query tabs."""

import pyarrow as pa
from textual.app import ComposeResult
from textual.content import Content
from textual.widgets import TabPane

from parqx.data.view import TableData
from parqx.tui.widgets.arrow_table import ArrowTable


class TablePane(TabPane):
    """Keep a table and its navigation state together until the tab closes."""

    DEFAULT_CSS = """
    TablePane {
        height: 1fr;
        & > ArrowTable { height: 1fr; }
    }
    """

    def __init__(
        self, title: str, *, id: str, table: pa.Table | TableData | None = None
    ) -> None:
        """Initialize a table or leave space for one to be mounted later."""
        super().__init__(Content(title), id=id)
        self.table = ArrowTable(table) if table is not None else None

    def compose(self) -> ComposeResult:
        """Yield the table when its data is already available."""
        if self.table is not None:
            yield self.table

    async def _mount_table(self, table: pa.Table | TableData) -> None:
        self.table = ArrowTable(table)
        await self.mount(self.table)

    def on_unmount(self) -> None:
        """Release Arrow buffers even if Textual briefly retains the closed widget."""
        if self.table is not None:
            self.table.replace_table(pa.table({}))
            self.table = None
