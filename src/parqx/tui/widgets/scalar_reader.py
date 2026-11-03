"""Read-only, viewport-sized rendering of complete scalar values."""

from __future__ import annotations

from functools import partial
from typing import ClassVar

import pyarrow as pa
from rich.segment import Segment
from textual.binding import Binding, BindingType
from textual.geometry import Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.worker import Worker

from parqx.tui.value_document import ReadingPosition, ScalarDocument, index_document


class ScalarReader(ScrollView, can_focus=True):
    """Keep native Arrow content and render only requested terminal lines."""

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

        def __init__(self, document: ScalarDocument, *, restoring: bool) -> None:
            """Identify the document so replacements can reject stale updates."""
            self.document = document
            self.restoring = restoring
            super().__init__()

    def __init__(self, *, classes: str) -> None:
        """Start with no document or outstanding formatting work."""
        super().__init__(classes=classes)
        self.document: ScalarDocument | None = None
        self._restore_y: float | None = None
        self._anchor: int | None = None
        self._to_end = False
        self._index_worker: Worker[None] | None = None

    @property
    def soft_wrap(self) -> bool:
        """Binary rows have their own width-sensitive layout."""
        return self.document is None or self.document.bytes_per_line is None

    @property
    def text(self) -> str:
        """Read the indexed document; viewport rendering never calls this."""
        document = self.document
        if document is None:
            return ""
        if document.bytes_per_line is not None:
            return "\n".join(document.line(i) for i in range(document.line_count))
        return "".join(document.raw_line(i) for i in range(document.line_count))

    def load_text(self, text: str) -> None:
        """Display a finite status message through the same read-only surface."""
        self.show(pa.scalar(text), width=max(1, self.scrollable_content_region.width))

    def show(self, value: pa.Scalar, *, width: int) -> None:
        """Replace the document and cancel any stale index builder."""
        self._cancel_index()
        self.document = ScalarDocument(value, width=width)
        self.document.advance()
        self._restore_y, self._anchor, self._to_end = None, None, False
        self.scroll_to(x=0, y=0, animate=False, force=True, immediate=True)
        self._update_size()
        self._start_indexing()

    def reflow(self, width: int) -> None:
        """Rewrap around the same source bytes instead of jumping to the start."""
        document = self.document
        if document is None or document.width == width:
            return
        position = self.capture_position()
        self.show(document.value, width=width)
        self.restore_position(position)

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
        restoring = self._anchor is not None or self._restore_y is not None
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
            self.scroll_to(y=target, animate=False, force=True, immediate=True)
            if document.complete or self.max_scroll_y >= target:
                self._restore_y = None
        self.refresh()
        self.post_message(self.Updated(document, restoring=restoring))

    def _start_indexing(self) -> None:
        if self.document is not None and not self.document.complete and self.is_mounted:
            self._index_worker = self.run_worker(
                partial(self._index, self.document), group="value-index", exclusive=True
            )

    def _cancel_index(self) -> None:
        if self._index_worker is not None:
            self._index_worker.cancel()
            self._index_worker = None

    async def _index(self, document: ScalarDocument) -> None:
        async for _ in index_document(document):
            if self.document is not document or not self.is_on_screen:
                break
            self._update_size()

    def on_show(self) -> None:
        """Resume indexing when this reader becomes visible again."""
        self._start_indexing()

    def on_hide(self) -> None:
        """Pause hidden formatting without releasing the selected native value."""
        self._cancel_index()

    def action_move(self, delta: int) -> None:
        """Scroll read-only content without moving an editing cursor."""
        self._anchor, self._restore_y, self._to_end = None, None, False
        self.scroll_to(y=self.scroll_y + delta, animate=False, immediate=True)

    def action_page(self, direction: int) -> None:
        """Move by one visible page."""
        self.action_move(direction * max(1, self.scrollable_content_region.height - 1))

    def action_first(self) -> None:
        """Return to the beginning and cancel pending end navigation."""
        self.action_move(-int(self.scroll_y))

    def action_last(self) -> None:
        """Follow the final line as incremental indexing completes."""
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
        return (
            Strip([Segment(document.line(line), style)])
            .adjust_cell_length(width, style)
            .crop(0, width)
        )
