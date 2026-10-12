"""Application budgets shared by SQL execution and source browsing."""

from dataclasses import dataclass
from math import isfinite


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
        if not isfinite(self.page_debounce_seconds) or self.page_debounce_seconds < 0:
            raise ValueError("page_debounce_seconds must be finite and nonnegative")
        if not self.memory_limit.strip():
            raise ValueError("memory_limit must be nonempty")
