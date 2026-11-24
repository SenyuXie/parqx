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

from parqx.tui.values._indexed_scroll_view import IndexedScrollView
from parqx.tui.values.document import ContainerDocument, ReadingPosition
from parqx.tui.values.node import ValueNode


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
    preview_styles: reactive[dict[str, Style]] = reactive(dict)
    """Foreground styles for visible field names and value previews."""
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
        self._page_lines = 0
        self._reveal = False
        self._restore_y: float | None = None

    def show(
        self, node: ValueNode, *, width: int, selected: int, styles: dict[str, Style]
    ) -> None:
        """Show direct children, retaining navigation when rewrapping the same selection."""
        if (
            self.document is None
            or self.document.node is not node
            or self.highlighted != selected
        ):
            self._cancel_pending_navigation()
            self.set_reactive(
                ValueChildren.highlighted, selected if node.child_count else None
            )
        self.preview_styles = styles
        self._index_at_width(node, width)

    def reflow(
        self,
        width: int,
        *,
        styles: dict[str, Style],
        position: ReadingPosition | None = None,
    ) -> bool:
        """Update preview styles and return whether the width required a new index."""
        self.preview_styles = styles
        document = self.document
        if document is not None and document.width != width:
            self._set_position(position or self.capture_position())
            self._index_at_width(document.node, width)
            return True
        return False

    def _index_at_width(self, node: ValueNode, width: int) -> None:
        self._cancel_index()
        self.document = ContainerDocument(node, width=width)
        self.document.advance()
        self._hovered = None
        self.tooltip = None
        self._update_size()
        self._start_indexing()

    def clear(self) -> None:
        """Release this view's document and cancel its index builder."""
        self._cancel_index()
        self.document = None
        self._cancel_pending_navigation()
        self.highlighted = None
        self.virtual_size = Size(0, 0)
        self.refresh()

    def _update_size(self) -> None:
        document = self.document
        if document is not None:
            self.virtual_size = Size(document.width, document.line_count)
            scroll = self._scroll_position(document)
            if scroll != self.scroll_y:
                self._place_position(scroll)
        self.refresh()

    def _scroll_position(self, document: ContainerDocument) -> float:
        """Resolve the viewport offset using only the lines indexed so far."""
        scroll = self.scroll_y
        height = self.scrollable_content_region.height
        max_scroll = self.max_scroll_y
        if self._restore_y is not None:
            scroll = min(self._restore_y, max_scroll)
            if document.complete or self._restore_y <= max_scroll:
                self._restore_y = None

        index = self.highlighted
        if index is None:
            return scroll

        paged = False
        if self._page_lines:
            target = document.page_target(
                index, self._page_lines, viewport_top=round(scroll)
            )
            if target is not None:
                scroll = max(0, min(scroll + self._page_lines, max_scroll))
                self._reveal_child(target)
                index = target
                paged = True

        if self._reveal and index < document.indexed_children:
            self._reveal = False
            line = document.child_line(index)
            child_height = document.node.child_height(index, document.width)
            window = Region(0, round(scroll), 1, height)
            child = Region(0, line, 1, child_height)
            if paged and window.overlaps(child):
                return scroll
            if index == document.node.child_count - 1:
                # Keep the closing delimiter visible beside the last child.
                child = Region(0, line, 1, child_height + 1)
            scroll += Region.get_scroll_to_visible(window, child).y
        return max(0, min(scroll, max_scroll))

    def _cancel_pending_navigation(self) -> None:
        self._page_lines = 0
        self._reveal = False
        self._restore_y = None

    def capture_position(self) -> ReadingPosition:
        """Capture the intended viewport, including pending navigation."""
        return ReadingPosition(
            scroll=self.scroll_y if self._restore_y is None else self._restore_y,
            reveal_selection=self._reveal or bool(self._page_lines),
        )

    def restore_position(self, position: ReadingPosition) -> None:
        """Resume a saved viewport as its indexed lines become available."""
        self._set_position(position)
        self._update_size()

    def _set_position(self, position: ReadingPosition) -> None:
        self._reveal = position.reveal_selection
        self._restore_y = None if self._reveal else position.scroll

    def _highlight(self, index: int) -> None:
        self.navigation_revision += 1
        self._reveal_child(index)
        self._update_size()

    def _reveal_child(self, index: int) -> None:
        self._page_lines = 0
        document = self.document
        if document is not None and document.node.child_count:
            index = max(0, min(index, document.node.child_count - 1))
            if self.highlighted != index:
                # Resolving a pending page is part of the original navigation.
                self.set_reactive(ValueChildren.highlighted, index)
                self.post_message(self.Highlighted(document, index))
            self._restore_y = None
            self._reveal = True

    def watch_highlighted(self, index: int | None) -> None:
        """Treat a programmatic highlight as navigation and notify the browser."""
        self.navigation_revision += 1
        if self.document is not None and index is not None:
            self.post_message(self.Highlighted(self.document, index))

    def action_move(self, delta: int) -> None:
        """Move by one child, independent of its visual height."""
        self._highlight((self.highlighted or 0) + delta)

    def action_first(self) -> None:
        """Select the first child and expose the opening delimiter."""
        self.navigation_revision += 1
        self._reveal_child(0)
        self._set_position(ReadingPosition())
        self._update_size()

    def action_last(self) -> None:
        """Select the absolute final child, including during field indexing."""
        if self.document is not None:
            self._highlight(self.document.node.child_count - 1)

    def action_page(self, direction: int) -> None:
        """Move by one viewport of rendered lines with one line of overlap."""
        self.navigation_revision += 1
        self._page_lines = direction * max(1, self.scrollable_content_region.height - 1)
        self._update_size()

    def action_select(self) -> None:
        """Open a child without requiring preceding values to be formatted."""
        self._page_lines = 0
        index = self.highlighted
        if self.document is not None and index is not None:
            self.post_message(self.Selected(self.document, index))

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
                index, width=document.width, styles=self.preview_styles
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
        return Strip(list(prompt.render(console, end=""))).adjust_cell_length(
            width, style
        )
