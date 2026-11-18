"""Schedule the viewport and selected-value reads for one source tab."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from weakref import ReferenceType, ref

from parqx.data.parquet import ParquetSource
from parqx.data.view import PageCache

if TYPE_CHECKING:
    from parqx.tui.widgets.value_browser import BrowseRoot


@dataclass(frozen=True)
class ReadContext:
    """Current UI state, borrowed only while making a scheduling decision."""

    source: ParquetSource
    cache: PageCache
    pending_root: BrowseRoot | None


@dataclass(frozen=True, eq=False)
class ReadRequest:
    """One native read tied to its original source and cache."""

    source: ParquetSource
    cache: ReferenceType[PageCache]
    start: int
    stop: int
    window_id: int | None = None
    root: BrowseRoot | None = None

    def matches(self, context: ReadContext) -> bool:
        """Check that the tab still displays the data this read was for."""
        return self.source is context.source and self.cache() is context.cache


class SourceReadSchedule:
    """Reserve one read slot and debounce the latest viewport independently."""

    def __init__(self) -> None:
        """Start with no requested window or submitted read."""
        self._window_id = 0
        self._pending_window: ReadRequest | None = None
        self._ready = False
        self._active: ReadRequest | None = None

    def invalidate_window(self) -> None:
        """Retire old viewport work while a submitted read keeps its slot."""
        self._window_id += 1
        self._pending_window = None
        self._ready = False

    def request_window(self, start: int, stop: int, context: ReadContext) -> int:
        """Replace the pending viewport and return its debounce timer's token."""
        self.invalidate_window()
        self._pending_window = ReadRequest(
            context.source,
            ref(context.cache),
            start,
            max(stop, start + context.source.page_rows),
            self._window_id,
        )
        return self._window_id

    def window_ready(self, window_id: int) -> bool:
        """Allow only the latest pending window's timer to make it eligible."""
        if self._pending_window is None or window_id != self._window_id:
            return False
        self._ready = True
        return True

    def take_next(self, context: ReadContext) -> ReadRequest | None:
        """Claim the next eligible read before it can queue in the executor."""
        if self._active is not None:
            return None
        request: ReadRequest | None
        root = context.pending_root
        if root is not None:
            request = ReadRequest(
                context.source, ref(context.cache), root.row, root.row + 1, root=root
            )
        else:
            request = self._pending_window
            if not self._ready or request is None:
                return None
            self._pending_window = None
            self._ready = False
            if not request.matches(context):
                return None
        self._active = request
        return request

    def finish(
        self, request: ReadRequest, context: ReadContext | None
    ) -> BrowseRoot | Literal["table", "discard"]:
        """Release the completed read and decide where its outcome belongs."""
        if self._active is not request:
            return "discard"
        self._active = None
        if context is None or not request.matches(context):
            return "discard"
        if request.root is not None:
            return request.root if request.root is context.pending_root else "discard"
        if request.window_id == self._window_id:
            return "table"
        return "discard"
