"""A virtual option list over the direct children of an Arrow value."""

from __future__ import annotations

from functools import partial
from typing import ClassVar

from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding, BindingType
from textual.content import Content
from textual.geometry import Region, Size
from textual.message import Message
from textual.reactive import reactive
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets.option_list import Option
from textual.worker import Worker

from parqx.tui.value_document import ContainerDocument, ReadingPosition, index_document
from parqx.tui.value_node import ValueNode


class ValueChildren(ScrollView, can_focus=True):
    """Render visible options only, retaining absolute positional identities."""

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
        self.document: ContainerDocument | None = None
        self.highlighted: int | None = None
        self._palette: dict[str, Style] = {}
        self._pending_selection = False
        self._restore_y: float | None = None
        self._index_worker: Worker[None] | None = None

    @property
    def option_count(self) -> int:
        """Virtual options, including unselectable delimiters."""
        if self.document is None:
            return 0
        count = self.document.node.child_count
        return count + 2 if count else 1

    def show(
        self, node: ValueNode, *, width: int, selected: int, styles: dict[str, Style]
    ) -> None:
        """Replace the current document and discard pending work for its predecessor."""
        self._cancel_index()
        self.document = ContainerDocument(node, width=width)
        self.document.advance()
        self.highlighted = selected + 1 if node.child_count else None
        self._palette = styles
        self._pending_selection = False
        self._restore_y = None
        self._hovered = None
        self.tooltip = None
        self._update_size()
        self._start_indexing()

    def clear_options(self) -> None:
        """Release this view's document and cancel its index builder."""
        self._cancel_index()
        self.document = None
        self.highlighted = None
        self._restore_y = None
        self.virtual_size = Size(0, 0)
        self.refresh()

    def get_option_at_index(self, index: int) -> Option:
        """Materialize one requested prompt, never a collection of all options."""
        return Option(
            self._get_prompt(index), disabled=index in {0, self.option_count - 1}
        )

    def _get_prompt(self, index: int) -> Text:
        document = self.document
        if document is None or not 0 <= index < self.option_count:
            raise IndexError(index)
        node = document.node
        left, right = node.delimiters
        if not node.child_count:
            return Text("  " + left + right)
        if index in {0, self.option_count - 1}:
            return Text("  " + (left if index == 0 else right))
        return node.child_prompt(
            index - 1,
            width=document.width,
            selected=index == self.highlighted,
            styles=self._palette,
        )

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

    def _start_indexing(self) -> None:
        if self.document is not None and not self.document.complete and self.is_mounted:
            self._index_worker = self.run_worker(
                partial(self._index, self.document), group="value-index", exclusive=True
            )

    def _cancel_index(self) -> None:
        if self._index_worker is not None:
            self._index_worker.cancel()
            self._index_worker = None

    async def _index(self, document: ContainerDocument) -> None:
        async for _ in index_document(document):
            if self.document is not document or not self.is_on_screen:
                break
            self._update_size()

    def on_show(self) -> None:
        """Resume indexing when a hidden workspace becomes visible."""
        self._start_indexing()

    def on_hide(self) -> None:
        """Stop background formatting while this body is hidden."""
        self._cancel_index()

    def _reveal_selection(self) -> None:
        document = self.document
        if document is None or self.highlighted is None:
            return
        index = self.highlighted - 1
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
        document = self.document
        if document is None or not document.node.child_count:
            return
        self.highlighted = max(1, min(index, document.node.child_count))
        self._restore_y = None
        self._reveal_selection()
        self.refresh()
        self.post_message(self.Highlighted(document, self.highlighted - 1))

    def action_move(self, delta: int) -> None:
        """Move by one child, independent of its visual height."""
        self._highlight((self.highlighted or 1) + delta)

    def action_first(self) -> None:
        """Select the first child and expose the opening delimiter."""
        self._highlight(1)
        self.scroll_to(y=0, animate=False, immediate=True)

    def action_last(self) -> None:
        """Select the absolute final child, including during field indexing."""
        self._highlight(self.option_count - 2)

    def action_page(self, direction: int) -> None:
        """Move by one viewport of children."""
        self.action_move(direction * max(1, self.scrollable_content_region.height - 1))

    def action_select(self) -> None:
        """Open a child without requiring preceding values to be formatted."""
        if self.document is not None and self.highlighted is not None:
            self.post_message(self.Selected(self.document, self.highlighted - 1))

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
        """Confirm the clicked child, matching the option-list interaction."""
        index = event.style.meta.get("value_child")
        if isinstance(index, int):
            event.stop()
            self._highlight(index + 1)
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
        if index is None:
            prompt = self._get_prompt(0 if line == 0 else self.option_count - 1)
        else:
            prompt = self._get_prompt(index + 1)
            prompt = prompt.split("\n")[line - document.child_line(index)]
            style += Style(meta={"value_child": index})
            if self.highlighted == index + 1:
                style += self.get_component_rich_style(
                    "option-list--option-highlighted"
                )
            elif self._hovered == index:
                style += self.get_component_rich_style("option-list--option-hover")
        prompt = prompt.copy()
        prompt.stylize_before(style)
        console = self.app.console  # pyright: ignore[reportUnknownMemberType]
        return (
            Strip(list(prompt.render(console, end="")))
            .adjust_cell_length(width, style)
            .crop(0, width)
        )
