"""The Parqx Textual App."""

import logging
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from textual import work
from textual.app import App, ComposeResult

from parqx.tui.widgets import ArrowTable, FileLoading

logger = logging.getLogger(__name__)


class ParqxApp(App[Any]):
    """A Textual App for Parqx."""

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
        """Yield the loading placeholder; ArrowTable mounts after read."""
        yield FileLoading(self._path)

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

    def _on_load_error(self, message: str) -> None:
        self.load_error = message
        self.exit(return_code=1)
