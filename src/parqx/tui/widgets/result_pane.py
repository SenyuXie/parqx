"""A completed SQL preview in its own tab."""

import pyarrow as pa

from parqx.tui.widgets.table_pane import TablePane


class ResultPane(TablePane):
    """Display a completed result without owning any source reads."""

    def __init__(self, title: str, *, id: str, table: pa.Table) -> None:
        """Initialize the result with its retained Arrow preview."""
        super().__init__(title, id=id, table=table)
