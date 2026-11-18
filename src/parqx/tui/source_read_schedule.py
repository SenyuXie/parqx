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
    """The pane's source, cache and browse root for one scheduling decision.

    A pending root is an explicit selection that still needs a source read.
    """

    source: ParquetSource
    cache: PageCache
    pending_root: BrowseRoot | None


@dataclass(frozen=True, eq=False)
class ReadRequest:
    """A read of rows [start, stop) for a viewport window or a browse root.

    The cache reference is weak so the pane can release it during a read.
    """

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
    """Share one source tab's read slot between its browse root and viewport.

    A pending browse root has priority when the slot becomes available.
    Replacing the viewport restarts its debounce interval.
    """

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
        """Replace the pending viewport and allow for page read-ahead.

        Args:
            start: First requested row, zero-based.
            stop: Exclusive end of the requested row range.
            context: Source and cache currently displayed by the pane.

        Returns:
            The window ID to pass to `window_ready` when its timer fires.
        """
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
        """Mark the window ready and return whether its timer is still current."""
        if self._pending_window is None or window_id != self._window_id:
            return False
        self._ready = True
        return True

    def take_next(self, context: ReadContext) -> ReadRequest | None:
        """Reserve the slot for a pending browse root or a ready viewport.

        Returns:
            The request to submit, or None if busy or no read is eligible.
            The request holds the slot even while queued in the executor.
        """
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
        """Release the active read's slot and route its success or failure.

        Args:
            request: The request returned by `take_next` for the completed read.
            context: Current pane state, or None if the source view is gone.

        Returns:
            The browse root to resolve or fail, "table" for the current viewport,
            or "discard" for an obsolete read. Apply this decision before taking
            the next read with fresh pane state.
        """
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
