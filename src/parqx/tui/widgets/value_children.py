"""A virtual view of the direct children of an Arrow value."""

from __future__ import annotations

from typing import ClassVar

from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding, BindingType
from textual.content import Content
from textual.geometry import Region, Size
from textual.message import Message
from textual.reactive import reactive
from textual.strip import Strip

from parqx.tui.values.document import ContainerDocument, ReadingPosition
from parqx.tui.values.node import ValueNode
from parqx.tui.widgets._indexed_scroll_view import IndexedScrollView


class ValueChildren(IndexedScrollView[ContainerDocument], can_focus=True):
    """Render visible children only, retaining absolute positional identities."""

    ALLOW_MAXIMIZE = True
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "option-list--option-highlighted",
        "option-list--option-hover",
    }
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move(-1)", "Previous", show=False),
        Binding("down", "move(1)", "Next", show=False),
        Binding("home", "first", "First", show=False),
        Binding("end", "last", "Last", show=False),
        Binding("pageup", "page(-1)", "Previous page", show=False),
        Binding("pagedown", "page(1)", "Next page", show=False),
        Binding("enter", "select", "Open", show=False),
    ]
    DEFAULT_CSS = "ValueChildren { overflow-x: hidden; }"

    highlighted: reactive[int | None] = reactive(None, init=False)
    """Zero-based child position, or None when no child is available."""
    _hovered: reactive[int | None] = reactive(None)

    class ChildMessage(Message):
        """Base message identifying a positional child in its document."""

        def __init__(self, document: ContainerDocument, index: int) -> None:
            """Capture a positional child and its owning document."""
            self.document, self.index = document, index
            super().__init__()

    class Highlighted(ChildMessage):
        """A positional child in a particular document was highlighted."""

    class Selected(ChildMessage):
        """The highlighted child was explicitly opened."""

    def __init__(self, *, classes: str) -> None:
        """Start with no retained node or child widgets."""
        super().__init__(classes=classes)
        self._palette: dict[str, Style] = {}
        self._page_lines = 0
        self._pending_selection = False
        self._restore_y: float | None = None

    def show(
        self, node: ValueNode, *, width: int, selected: int, styles: dict[str, Style]
    ) -> None:
        """Replace the current document and discard pending work for its predecessor."""
        self._cancel_index()
        self.document = ContainerDocument(node, width=width)
        self.document.advance()
        self.set_reactive(
            ValueChildren.highlighted, selected if node.child_count else None
        )
        self._palette = styles
        self._page_lines = 0
        self._pending_selection = False
        self._restore_y = None
        self._hovered = None
        self.tooltip = None
        self._update_size()
        self._start_indexing()

    def clear(self) -> None:
        """Release this view's document and cancel its index builder."""
        self._cancel_index()
        self.document = None
        self._page_lines = 0
        self.highlighted = None
        self._restore_y = None
        self.virtual_size = Size(0, 0)
        self.refresh()

    def _update_size(self) -> None:
        document = self.document
        if document is not None:
            self.virtual_size = Size(document.width, document.line_count)
            if self._restore_y is not None:
                self.scroll_to(
                    y=self._restore_y, animate=False, force=True, immediate=True
                )
                if document.complete or self.max_scroll_y >= self._restore_y:
                    self._restore_y = None
            self._apply_pending_page()
            if self._pending_selection:
                self._reveal_selection()
        self.refresh()

    def capture_position(self) -> ReadingPosition:
        """Keep a pending parent viewport or End navigation across visibility changes."""
        scroll = self.scroll_y if self._restore_y is None else self._restore_y
        return ReadingPosition(scroll=scroll, reveal_selection=self._pending_selection)

    def restore_position(self, position: ReadingPosition) -> None:
        """Restore immediately, or as soon as its indexed children become available."""
        self._restore_y = position.scroll
        self._pending_selection = position.reveal_selection
        self._update_size()

    def _reveal_selection(self) -> None:
        document = self.document
        if document is None or self.highlighted is None:
            return
        index = self.highlighted
        if index >= document.indexed_children:
            self._pending_selection = True
            return
        self._pending_selection = False
        y = document.child_line(index)
        height = document.node.child_height(index, document.width)
        if index == document.node.child_count - 1:
            height += 1  # Keep the closing delimiter visible beside the last child.
        self.scroll_to_region(
            Region(0, y, 1, height), animate=False, force=True, immediate=True
        )

    def _highlight(self, index: int) -> None:
        self._page_lines = 0
        document = self.document
        if document is None or not document.node.child_count:
            return
        self.highlighted = max(0, min(index, document.node.child_count - 1))
        self._restore_y = None
        self._reveal_selection()

    def watch_highlighted(self, index: int | None) -> None:
        """Notify the browser when a different child becomes highlighted."""
        if self.document is not None and index is not None:
            self.post_message(self.Highlighted(self.document, index))

    def action_move(self, delta: int) -> None:
        """Move by one child, independent of its visual height."""
        self._highlight((self.highlighted or 0) + delta)

    def action_first(self) -> None:
        """Select the first child and expose the opening delimiter."""
        self._highlight(0)
        self.scroll_to(y=0, animate=False, immediate=True)

    def action_last(self) -> None:
        """Select the absolute final child, including during field indexing."""
        if self.document is not None:
            self._highlight(self.document.node.child_count - 1)

    def action_page(self, direction: int) -> None:
        """Move by one viewport of rendered lines with one line of overlap."""
        self._page_lines = direction * max(1, self.scrollable_content_region.height - 1)
        self._apply_pending_page()

    def _apply_pending_page(self) -> None:
        if (
            self._page_lines
            and self.document is not None
            and self.highlighted is not None
        ):
            target = self.document.page_target(self.highlighted, self._page_lines)
            if target is not None:
                self._highlight(target)

    def action_select(self) -> None:
        """Open a child without requiring preceding values to be formatted."""
        self._page_lines = 0
        if self.document is not None and self.highlighted is not None:
            self.post_message(self.Selected(self.document, self.highlighted))

    def on_mouse_move(self, event: events.MouseMove) -> None:
        """Expose complete identity and a full-width hover background."""
        label = event.style.meta.get("value_tooltip")
        self.tooltip = Content(label) if isinstance(label, str) else None
        hovered = event.style.meta.get("value_child")
        self._hovered = hovered if isinstance(hovered, int) else None

    def on_leave(self) -> None:
        """Discard hover state after leaving the body."""
        self.tooltip = None
        self._hovered = None

    def on_click(self, event: events.Click) -> None:
        """Highlight and open the clicked child."""
        index = event.style.meta.get("value_child")
        if isinstance(index, int):
            event.stop()
            self._highlight(index)
            self.action_select()

    def render_line(self, y: int) -> Strip:
        """Render only the requested viewport line with its absolute child index."""
        width = self.scrollable_content_region.width
        style = self.rich_style
        document = self.document
        line = y + self.scroll_offset.y
        if document is None or line >= document.line_count:
            return Strip.blank(width, style)
        index = document.child_at_line(line)
        node = document.node
        if index is None:
            left, right = node.delimiters
            if not node.child_count:
                delimiter = left + right
            elif line == 0:
                delimiter = left
            else:
                delimiter = right
            prompt = Text("  " + delimiter)
        else:
            prompt = node.child_prompt(
                index, width=document.width, styles=self._palette
            )
            prompt = prompt.split("\n")[line - document.child_line(index)]
            style += Style(meta={"value_child": index})
            if self._hovered == index:
                style += self.get_component_rich_style("option-list--option-hover")
            if self.highlighted == index:
                text = prompt.plain
                prompt.stylize(
                    self.get_component_rich_style("option-list--option-highlighted"),
                    start=len(text) - len(text.lstrip(" ")),
                    end=len(text.rstrip(" ")),
                )
        prompt.stylize_before(style)
        console = self.app.console  # pyright: ignore[reportUnknownMemberType]
        return (
            Strip(list(prompt.render(console, end="")))
            .adjust_cell_length(width, style)
            .crop(0, width)
        )
