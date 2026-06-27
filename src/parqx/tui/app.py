"""The Parqx Textual App."""

from typing import Any

import pyarrow as pa
from textual.app import App, ComposeResult

from parqx.tui.widgets import ArrowTable


class ParqxApp(App[Any]):
    """A Textual App for Parqx."""

    def __init__(self, table: pa.Table) -> None:
        """Initialize the app with an Arrow table to inspect.

        Args:
            table: Arrow table displayed by the main table widget.
        """
        super().__init__()
        self._table = table

    def compose(self) -> ComposeResult:
        """Yield child widgets for the app."""
        yield ArrowTable(self._table)
