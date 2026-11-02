"""One table and its independently owned value browser."""

from typing import ClassVar

import pyarrow as pa
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.coordinate import Coordinate

from parqx.data.view import PageCache
from parqx.tui.value_node import RowValue
from parqx.tui.widgets.arrow_table import ArrowTable, CellNotLoadedError
from parqx.tui.widgets.value_browser import BrowseRoot, ValueBrowser


class TableView(Horizontal):
    """Share the table-height layout and selection rules across source and SQL."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("v", "toggle_browser", "Value"),
        Binding("escape", "return_to_table", show=False, priority=True),
        Binding("backspace", "browse_parent", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    TableView {
        height: 1fr;
        & > ArrowTable { width: 1fr; height: 1fr; }
    }
    """

    def __init__(self, table: ArrowTable) -> None:
        """Own a table's explicit selection for its lifetime."""
        super().__init__()
        self.table = table
        self.browser = ValueBrowser()
        self.browser.clear(empty=table.row_count == 0)
        self._identity = object()

    def compose(self) -> ComposeResult:
        """Keep the browser immediately after the table in focus order."""
        yield self.table
        yield self.browser

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Apply reader keys only within this table's own focus region."""
        if action in {"return_to_table", "browse_parent"}:
            return self.browser.has_focus_within
        return super().check_action(action, parameters)

    def action_toggle_browser(self) -> None:
        """Hide or resume the selected root without sampling the cursor."""
        if self.browser.has_focus_within:
            self.table.focus()
        self.browser.toggle_visibility()

    def action_return_to_table(self) -> None:
        """Leave the root reader open while returning to the table."""
        self.table.focus()

    def action_browse_parent(self) -> None:
        """Return to the parent value, or to the table when at the root."""
        if not self.browser.back():
            self.table.focus()

    @on(ArrowTable.CellSelected)
    def _select_cell(self, event: ArrowTable.CellSelected) -> None:
        event.stop()
        if event.control is not self.table or event.generation != self.table.generation:
            return
        coordinate = event.coordinate
        self.browser.select(
            BrowseRoot(
                self._identity,
                event.generation,
                coordinate.row,
                coordinate.column,
                self.table.columns[coordinate.column].name,
                event.value,
            )
        )

    def replace_data(self, data: pa.Table | PageCache) -> None:
        """Invalidate the selected root without changing visibility or focus."""
        self.table.replace_data(data)
        self.browser.clear(empty=self.table.row_count == 0)

    @on(ArrowTable.RowSelected)
    def _select_row(self, event: ArrowTable.RowSelected) -> None:
        event.stop()
        if event.control is self.table and event.generation == self.table.generation:
            try:
                fields = tuple(
                    (
                        field.name,
                        self.table.get_cell_at(Coordinate(event.cursor_row, column)),
                    )
                    for column, field in enumerate(self.table.data.schema)
                )
            except CellNotLoadedError:
                return
            self.browser.select(
                BrowseRoot(
                    self._identity,
                    event.generation,
                    event.cursor_row,
                    None,
                    "",
                    RowValue(fields),
                )
            )

    def on_unmount(self) -> None:
        """Release retained scalar buffers even if the view remains referenced."""
        self.browser.clear(empty=True)
