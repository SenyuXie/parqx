"""Reading positions for the active frame of a value browser."""

from collections.abc import Callable
from dataclasses import dataclass, field

from textual.widget import Widget

from parqx.tui.values.children import ValueChildren
from parqx.tui.values.document import ReadingPosition
from parqx.tui.values.node import ValueNode
from parqx.tui.values.reader import ScalarReader


@dataclass
class BrowseFrame:
    """A visited value and the reading position to restore on return."""

    label: str
    node: ValueNode
    selected: int = 0
    position: ReadingPosition = field(default_factory=ReadingPosition)


@dataclass
class _MaximizedPosition:
    """A bookmark and the scroll offset where the enlarged viewport settled."""

    position: ReadingPosition
    selected: int
    enlarged_scroll: float | None = None


class BrowseViewport:
    """Preserve the active frame's reading position across layout changes.

    The reader captures positions in its own units. A frame change discards
    deferred work, while hiding the current frame preserves its destination
    even when its document is still being indexed.
    """

    def __init__(
        self,
        owner: Widget,
        children: ValueChildren,
        scalar: ScalarReader,
        reflow: Callable[[ReadingPosition | None], None],
    ) -> None:
        """Connect the two readers to the browser's width-dependent layout.

        Args:
            owner: Widget whose layout determines the reader dimensions.
            children: Reader for container values.
            scalar: Reader for scalar values and browser status text.
            reflow: Rewrap the active document, using the supplied position
                when resuming a saved viewport.
        """
        self._owner = owner
        self._children = children
        self._scalar = scalar
        self._reflow = reflow
        self._frame: BrowseFrame | None = None
        self._restore: ReadingPosition | None = None
        self._maximized: _MaximizedPosition | None = None
        self._revision = 0

    @property
    def body(self) -> ValueChildren | ScalarReader:
        """The reader displayed for the active frame or status text."""
        return (
            self._children
            if self._frame is not None and self._frame.node.is_container
            else self._scalar
        )

    def show(self, frame: BrowseFrame | None) -> None:
        """Activate a browse frame, or clear the selection and its pending work."""
        self._revision += 1
        self._maximized = None
        self._frame = frame
        self._restore = None
        if frame is not None:
            self.body.restore_position(frame.position)
            self.body.call_after_refresh(self._restore_frame, self._revision)

    def save(self) -> None:
        """Remember the intended position before leaving or hiding a frame."""
        self._revision += 1
        if self._frame is not None:
            self._frame.position = self._restore or self.body.capture_position()
        self._restore = None

    def resume(self) -> None:
        """Reapply the saved position after Textual lays out the visible reader."""
        if self._frame is not None:
            self._restore = self._frame.position
        self._owner.call_after_refresh(self._settle, self._revision)

    def set_maximized(self, maximized: bool) -> None:
        """Remember a position on maximize and restore it when still applicable.

        Args:
            maximized: Whether the table and its sidebar now fill the screen.
        """
        frame = self._frame
        if frame is None:
            return
        body = self.body
        saved = self._maximized
        if maximized:
            self._maximized = _MaximizedPosition(
                self._restore or body.capture_position(), frame.selected
            )
        else:
            self._maximized = None
            if (
                saved is not None
                and frame.selected == saved.selected
                and body.scroll_y == saved.enlarged_scroll
            ):
                self._restore = saved.position
                frame.position = saved.position
        self._revision += 1
        self._owner.call_after_refresh(self._settle, self._revision)

    def scalar_restored(self, scroll: float) -> None:
        """Record where a pending byte anchor settled in the enlarged reader."""
        if self._maximized is not None and self._maximized.enlarged_scroll is not None:
            self._maximized.enlarged_scroll = min(scroll, self._scalar.max_scroll_y)

    def layout_changed(self) -> None:
        """Reflow after layout, ignoring callbacks superseded by a frame change."""
        self._owner.call_after_refresh(self._reflow_current, self._revision)

    def _reflow_current(self, revision: int) -> None:
        if revision == self._revision and self._owner.is_on_screen:
            self._reflow(self._restore)

    def _restore_frame(self, revision: int) -> None:
        if (
            revision == self._revision
            and self._frame is not None
            and self._owner.display
        ):
            self.body.restore_position(self._frame.position)

    def _settle(self, revision: int) -> None:
        if revision != self._revision or not self._owner.is_on_screen:
            return
        self._reflow(self._restore)
        if self._restore is not None:
            self.body.restore_position(self._restore)
            self._restore = None
        if self._maximized is not None and self._maximized.enlarged_scroll is None:
            self._maximized.enlarged_scroll = self.body.scroll_y
