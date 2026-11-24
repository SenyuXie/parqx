"""Navigation through the rendered lines of a container value."""

from textual.geometry import Region

from parqx.tui.values.document import ContainerDocument, ReadingPosition


class ChildNavigation:
    """Keep the highlighted child and its viewport together as the index grows.

    One instance belongs to one container. Rewrapping the same container supplies
    a new document to `scroll_position`, preserving any outstanding page request.
    """

    def __init__(self, child_count: int, selected: int = 0) -> None:
        """Initialize navigation at the selected child.

        Args:
            child_count: Number of direct children in the container.
            selected: Zero-based position to highlight when children are present.
        """
        self._child_count = child_count
        self.highlighted = selected if child_count else None
        self._page_lines = 0
        self._reveal = False
        self._restore_y: float | None = None

    def highlight(self, index: int) -> None:
        """Highlight a child and reveal it when its lines are available."""
        self._page_lines = 0
        if self._child_count:
            self.highlighted = max(0, min(index, self._child_count - 1))
            self._restore_y = None
            self._reveal = True

    def first(self) -> None:
        """Highlight the first child and include the opening delimiter."""
        self.highlight(0)
        self.restore(ReadingPosition())

    def page(self, direction: int, *, height: int) -> None:
        """Request a page of rendered lines with one line of overlap."""
        self._page_lines = direction * max(1, height - 1)

    def select(self) -> int | None:
        """Get the child to open, superseding a pending page."""
        self._page_lines = 0
        return self.highlighted

    def cancel_pending(self) -> None:
        """Keep the highlighted child and let the user scroll away from it."""
        self._page_lines = 0
        self._reveal = False
        self._restore_y = None

    def capture(self, scroll: float) -> ReadingPosition:
        """Save the intended position, even if indexing has yet to reach it."""
        return ReadingPosition(
            scroll=scroll if self._restore_y is None else self._restore_y,
            reveal_selection=self._reveal or bool(self._page_lines),
        )

    def restore(self, position: ReadingPosition) -> None:
        """Resume a saved viewport or follow the highlighted child."""
        self._reveal = position.reveal_selection
        self._restore_y = None if self._reveal else position.scroll

    def scroll_position(
        self,
        document: ContainerDocument,
        *,
        scroll: float,
        height: int,
        max_scroll: int,
    ) -> float:
        """Resolve pending navigation against the currently indexed lines.

        Args:
            document: Line index for the container at its current width.
            scroll: Current vertical scroll offset.
            height: Number of visible lines.
            max_scroll: Largest vertical scroll offset accepted by the viewport.

        Returns:
            The viewport offset for the current highlighted child.
        """
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
                self.highlight(target)
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
            if index == self._child_count - 1:
                # Keep the closing delimiter visible beside the last child.
                child = Region(0, line, 1, child_height + 1)
            scroll += Region.get_scroll_to_visible(window, child).y
        return max(0, min(scroll, max_scroll))
