"""Immutable session budgets and table preferences."""

from dataclasses import dataclass
from math import isfinite
from typing import Literal

type CursorType = Literal["cell", "row", "column", "none"]


@dataclass(frozen=True, kw_only=True)
class TableDefaults:
    """Display preferences applied when a table is created."""

    max_column_content_width: int = 48
    cursor_type: CursorType = "cell"
    zebra_stripes: bool = False
    show_row_index: bool = True
    show_header: bool = True

    def __post_init__(self) -> None:
        """Validate settings before constructing a table."""
        if (
            type(self.max_column_content_width) is not int
            or self.max_column_content_width < 2
        ):
            raise ValueError(
                "max_column_content_width must be an integer of at least 2"
            )
        if self.cursor_type not in ("cell", "row", "column", "none"):
            raise ValueError("cursor_type must be cell, row, column, or none")
        for name in ("zebra_stripes", "show_row_index", "show_header"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True, kw_only=True)
class ParqxConfig:
    """Immutable policy values; construction performs no I/O.

    DuckDB memory is per connection. Preview, page and cache budgets independently
    bound retained Arrow data; each allows one oversized row or page for progress.
    DuckDB validates the memory-limit syntax when opening a connection.
    """

    memory_limit: str = "256MB"
    threads: int = 2
    preview_rows: int = 10_000
    preview_bytes: int = 32 * 1024 * 1024
    query_batch_rows: int = 1024
    page_rows: int = 4096
    page_bytes: int = 4 * 1024 * 1024
    page_cache_bytes: int = 32 * 1024 * 1024
    source_workers: int = 4
    page_debounce_seconds: float = 0.1
    table: TableDefaults = TableDefaults()

    def __post_init__(self) -> None:
        """Reject budgets that cannot make progress and invalid timer intervals."""
        for name in (
            "threads",
            "preview_rows",
            "preview_bytes",
            "query_batch_rows",
            "page_rows",
            "page_bytes",
            "page_cache_bytes",
            "source_workers",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.threads >= 2**63:
            raise ValueError("threads must fit in a DuckDB BIGINT")
        if not isfinite(self.page_debounce_seconds) or self.page_debounce_seconds < 0:
            raise ValueError("page_debounce_seconds must be finite and nonnegative")
        if not self.memory_limit.strip():
            raise ValueError("memory_limit must be nonempty")
