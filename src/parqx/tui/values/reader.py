"""Read-only, viewport-sized rendering of complete scalar values."""

from __future__ import annotations

from typing import ClassVar

import pyarrow as pa
from rich.segment import Segment
from textual.binding import Binding, BindingType
from textual.geometry import Size
from textual.message import Message
from textual.strip import Strip

from parqx.tui.values._indexed_scroll_view import IndexedScrollView
from parqx.tui.values.document import ReadingPosition, ScalarDocument


class ScalarReader(IndexedScrollView[ScalarDocument], can_focus=True):
    """Display a ScalarDocument with read-only scrolling and keyboard navigation.

    ValueBrowser supplies the value and width through show(). This widget owns
    indexing and scrolling, drawing visible rows with document.line().
    """

    ALLOW_MAXIMIZE = True
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move(-1)", "Up", show=False),
        Binding("down", "move(1)", "Down", show=False),
        Binding("pageup", "page(-1)", "Page up", show=False),
        Binding("pagedown", "page(1)", "Page down", show=False),
        Binding("home,ctrl+home", "first", "Start", show=False),
        Binding("end,ctrl+end", "last", "End", show=False),
    ]
    DEFAULT_CSS = "ScalarReader { overflow-x: hidden; }"
    read_only = True

    class Updated(Message):
        """The current document's index or metadata has advanced."""

        def __init__(self, document: ScalarDocument) -> None:
            """Identify the document so replacements can reject stale updates."""
            self.document = document
            super().__init__()

    def __init__(self, *, classes: str) -> None:
        """Start with no document or outstanding formatting work."""
        super().__init__(classes=classes)
        self._restore_y: float | None = None
        self._anchor: int | None = None
        self._to_end = False

    @property
    def soft_wrap(self) -> bool:
        """Binary rows have their own width-sensitive layout."""
        return self.document is None or self.document.bytes_per_line is None

    @property
    def text(self) -> str:
        """Materialize the currently indexed content for inspection.

        Rendering reads individual rows through document.line().
        """
        document = self.document
        if document is None:
            return ""
        if document.bytes_per_line is not None:
            return "\n".join(document.line(i) for i in range(document.line_count))
        return "".join(document.raw_line(i) for i in range(document.line_count))

    def show(self, value: pa.Scalar, *, width: int) -> None:
        """Replace the document, prepare its first batch and restart indexing."""
        self._cancel_index()
        self.document = ScalarDocument(value, width=width)
        self.document.advance()
        self._cancel_pending_navigation()
        self._place_position(0, x=0)
        self._update_size()
        self._start_indexing()

    def clear(self) -> None:
        """Release the selected scalar and any unfinished indexing."""
        self._cancel_index()
        self.document = None
        self._cancel_pending_navigation()
        self.virtual_size = Size(0, 0)
        self._place_position(0, x=0)
        self.refresh()

    def reflow(self, width: int) -> None:
        """Rewrap around the same source bytes instead of jumping to the start."""
        document = self.document
        if document is None or document.width == width:
            return
        position = self.capture_position()
        self.show(document.value, width=width)
        self.restore_position(position)

    def _cancel_pending_navigation(self) -> None:
        self._anchor, self._restore_y, self._to_end = None, None, False

    def capture_position(self) -> ReadingPosition:
        """Save the intended source position instead of a temporarily clamped row."""
        scroll = self.scroll_y if self._restore_y is None else self._restore_y
        anchor = self._anchor
        if (
            anchor is None
            and self.document is not None
            and scroll < self.document.line_count
        ):
            anchor = self.document.byte_offset(int(scroll))
        return ReadingPosition(scroll, anchor, self._to_end)

    def restore_position(self, position: ReadingPosition) -> None:
        """Resume source-anchored reading after hiding, resizing or returning."""
        self._anchor = position.byte_offset
        self._restore_y = position.scroll if self._anchor is None else None
        self._to_end = position.follow_end
        self._update_size()

    def _update_size(self) -> None:
        document = self.document
        if document is None:
            return
        self.virtual_size = Size(document.width, document.line_count)
        target = self._restore_y
        if self._anchor is not None:
            target = document.line_at_byte(self._anchor)
            if document.complete or (
                document.byte_offset(max(0, document.line_count - 1)) >= self._anchor
                and self.max_scroll_y >= target
            ):
                self._anchor = None
        if self._to_end:
            target = self.max_scroll_y
            if document.complete:
                self._to_end = False
        if target is not None:
            self._place_position(target)
            if document.complete or self.max_scroll_y >= target:
                self._restore_y = None
        self.refresh()
        self.post_message(self.Updated(document))

    def action_move(self, delta: int) -> None:
        """Scroll read-only content without moving an editing cursor."""
        self.navigation_revision += 1
        self._cancel_pending_navigation()
        self._place_position(self.scroll_y + delta)

    def action_page(self, direction: int) -> None:
        """Move by one visible page."""
        self.action_move(direction * max(1, self.scrollable_content_region.height - 1))

    def action_first(self) -> None:
        """Return to the beginning and cancel pending end navigation."""
        self.action_move(-int(self.scroll_y))

    def action_last(self) -> None:
        """Follow the final line as incremental indexing completes."""
        self.navigation_revision += 1
        self._anchor, self._restore_y, self._to_end = None, None, True
        self._update_size()

    def render_line(self, y: int) -> Strip:
        """Decode and format only one requested visible line."""
        width = self.scrollable_content_region.width
        document = self.document
        line = y + self.scroll_offset.y
        style = self.rich_style
        if document is None or line >= document.line_count:
            return Strip.blank(width, style)
        return Strip([Segment(document.line(line), style)]).adjust_cell_length(
            width, style
        )
