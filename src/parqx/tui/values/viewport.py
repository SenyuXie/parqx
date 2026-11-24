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


@dataclass(frozen=True)
class _Bookmark:
    """A destination valid until the reader receives newer navigation."""

    position: ReadingPosition
    navigation_revision: int


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
        self._restore: _Bookmark | None = None
        self._maximized: _Bookmark | None = None
        self._revision = 0

    @property
    def body(self) -> ValueChildren | ScalarReader:
        """The reader displayed for the active frame or status text."""
        return (
            self._children
            if self._frame is not None and self._frame.node.is_container
            else self._scalar
        )

    def _destination(self) -> ReadingPosition | None:
        bookmark = self._restore
        if (
            bookmark is not None
            and bookmark.navigation_revision == self.body.navigation_revision
        ):
            return bookmark.position
        return None

    def show(self, frame: BrowseFrame | None) -> None:
        """Activate a browse frame, or clear the selection and its pending work."""
        self._revision += 1
        self._maximized = None
        self._frame = frame
        self._restore = None
        if frame is not None:
            self.body.restore_position(frame.position)
            self._restore = _Bookmark(frame.position, self.body.navigation_revision)
            self.body.call_after_refresh(self._restore_frame, self._revision)

    def save(self) -> None:
        """Remember the intended position before leaving or hiding a frame."""
        self._revision += 1
        if self._frame is not None:
            self._frame.position = self._destination() or self.body.capture_position()
        self._restore = None

    def resume(self) -> None:
        """Reapply the saved position after Textual lays out the visible reader."""
        if self._frame is not None:
            self._restore = _Bookmark(
                self._frame.position, self.body.navigation_revision
            )
        self._owner.call_after_refresh(self._settle, self._revision)

    def set_maximized(self, maximized: bool) -> None:
        """Remember a position on maximize and restore it when still applicable.

        Args:
            maximized: Whether the table and its sidebar now fill the screen.
        """
        frame = self._frame
        if frame is None:
            return
        if maximized:
            self._maximized = _Bookmark(
                self._destination() or self.body.capture_position(),
                self.body.navigation_revision,
            )
        elif self._maximized is not None:
            self._restore = self._maximized
            self._maximized = None
            if (position := self._destination()) is not None:
                frame.position = position
        self._revision += 1
        self._owner.call_after_refresh(self._settle, self._revision)

    def layout_changed(self) -> None:
        """Reflow after layout, ignoring callbacks superseded by a frame change."""
        self._owner.call_after_refresh(self._reflow_current, self._revision)

    def _reflow_current(self, revision: int) -> None:
        if revision == self._revision and self._owner.is_on_screen:
            self._reflow(self._destination())

    def _restore_frame(self, revision: int) -> None:
        if revision == self._revision and self._owner.display:
            position = self._destination()
            if position is not None:
                self.body.restore_position(position)
            self._restore = None

    def _settle(self, revision: int) -> None:
        if revision == self._revision and self._owner.is_on_screen:
            self._reflow(self._destination())
            self._restore_frame(revision)
