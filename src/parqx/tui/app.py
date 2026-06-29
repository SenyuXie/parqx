"""The Parqx Textual App."""

import logging
from pathlib import Path
from typing import Any, ClassVar

import pyarrow as pa
import pyarrow.parquet as pq
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.css.query import NoMatches
from textual.widgets import Footer

from parqx.tui.widgets import ArrowTable, FileLoading
from parqx.tui.widgets.arrow_table import CursorType

logger = logging.getLogger(__name__)


class ParqxApp(App[Any]):
    """A Textual App for Parqx."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("h", "toggle_header", "Header", show=True),
        Binding("i", "toggle_row_index", "Index", show=True),
        Binding("z", "toggle_zebra", "Zebra", show=True),
        Binding("c", "cycle_cursor_type", "Cursor", show=True),
    ]

    _CURSOR_TYPE_CYCLE: ClassVar[tuple[CursorType, ...]] = (
        "cell",
        "row",
        "column",
        "none",
    )
    """Order in which `action_cycle_cursor_type` advances the cursor type."""

    def __init__(self, path: Path) -> None:
        """Initialize the app with a Parquet file path to inspect.

        Args:
            path: Parquet file shown by the main table widget. The file is read
                asynchronously after the UI mounts, not in this constructor.
        """
        super().__init__()
        self._path = path
        self.load_error: str | None = None
        """Set when the worker thread fails to read the file. The CLI inspects
        this after `run` returns to decide between a clean exit and a non-zero
        exit with an error message."""

    def compose(self) -> ComposeResult:
        """Yield the loading placeholder plus a persistent footer.

        The body widget (`FileLoading`, later swapped for `ArrowTable`) is the
        only thing that gets mounted/removed. `Footer` is docked to the bottom
        and persists for the app's lifetime so its key hints are always visible.
        """
        yield FileLoading(self._path)
        yield Footer(show_command_palette=True)

    def on_mount(self) -> None:
        """Kick off the parquet read as soon as the loading UI is visible."""
        self._load_table()

    @work(thread=True, exclusive=True)
    def _load_table(self) -> None:
        try:
            table: pa.Table = pq.read_table(self._path)  # type: ignore
        except (OSError, pa.ArrowException, MemoryError) as exc:
            logger.exception("Failed to read parquet file: %s", self._path)
            self.call_from_thread(self._on_load_error, str(exc))
            return
        self.call_from_thread(self._on_load_ok, table)

    def _on_load_ok(self, table: pa.Table) -> None:
        self.query_one(FileLoading).remove()
        self.mount(ArrowTable(table))
        self.query_one(ArrowTable).focus()

    def _on_load_error(self, message: str) -> None:
        self.load_error = message
        self.exit(return_code=1)

    def _get_arrow_table(self) -> ArrowTable | None:
        """Return the mounted `ArrowTable`, or `None` during the loading phase."""
        try:
            return self.query_one(ArrowTable)
        except NoMatches:
            return None

    def action_toggle_header(self) -> None:
        """Toggle the visibility of the column header row."""
        if (table := self._get_arrow_table()) is not None:
            table.show_header = not table.show_header

    def action_toggle_row_index(self) -> None:
        """Toggle the visibility of the row-index column."""
        if (table := self._get_arrow_table()) is not None:
            table.show_row_index = not table.show_row_index

    def action_toggle_zebra(self) -> None:
        """Toggle zebra striping on data rows."""
        if (table := self._get_arrow_table()) is not None:
            table.zebra_stripes = not table.zebra_stripes

    def action_cycle_cursor_type(self) -> None:
        """Advance the table's cursor type through `_CURSOR_TYPE_CYCLE`."""
        table = self._get_arrow_table()
        if table is None:
            return
        cycle = self._CURSOR_TYPE_CYCLE
        next_index = (cycle.index(table.cursor_type) + 1) % len(cycle)
        table.cursor_type = cycle[next_index]
