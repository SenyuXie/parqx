"""Read an explicitly selected Arrow value without using table previews."""

from dataclasses import dataclass, field, replace
from typing import ClassVar

import pyarrow as pa
from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import HorizontalScroll, Vertical, VerticalScroll
from textual.content import Content
from textual.widget import Widget
from textual.widgets import Button, ContentSwitcher, Static

from parqx.tui.values.children import ValueChildren
from parqx.tui.values.document import ReadingPosition
from parqx.tui.values.node import BrowseRoot, RowValue, ValueNode
from parqx.tui.values.reader import ScalarReader


@dataclass
class _BrowseFrame:
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


def _label_text(label: str) -> Text:
    """Keep field names on one visible line without interpreting markup."""
    return Text(label.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))


class ValueBrowser(Vertical):
    """Keep selected content and its reading position while hidden."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "value--key",
        "value--string",
        "value--number",
        "value--boolean",
        "value--muted",
    }

    DEFAULT_CSS = """
    ValueBrowser {
        width: 33%; height: 1fr;
        border-left: solid $primary-muted;
        padding: 1 1 0 1; background: $surface;
        &:focus-within {
            border-left: solid $primary;
            background-tint: $foreground 5%;
        }
        .value--key { color: #1d4b70; }
        .value--string, .value-reader.string { color: #19512f; }
        .value--number, .value-reader.number { color: #6b3a0e; }
        .value--boolean, .value-reader.boolean { color: #533975; }
        .value--muted, .value-status.muted, .value-reader.muted { color: #3e4856; }
        .value-heading {
            height: 1; padding: 0 1; color: $text-muted; text-style: bold;
        }
        .value-path {
            height: 2; margin-top: 1; padding: 0 1;
            scrollbar-size-horizontal: 1;
        }
        .value-identity { width: auto; height: 1; text-style: bold; }
        .value-crumb {
            width: auto; min-width: 1; height: 1; padding: 0;
            border: none; background: transparent; color: $text-muted;
            text-style: none;
            &:hover { background: $boost; color: $text; }
            &.current { text-style: bold; color: $text; }
        }
        .value-separator { width: 3; height: 1; color: $text-muted; }
        .value-type {
            height: 2; padding: 0 1; margin-bottom: 1;
            color: $text-muted; border-bottom: solid $foreground 20%;
        }
        .value-bodies { height: 1fr; }
        .value-reader, .value-status {
            height: 1fr; padding: 0 2; border: none; background: transparent;
            &:focus { border: none; }
            &.error { color: $error; }
        }
        .value-children {
            height: 1fr; padding: 0; border: none; background: transparent;
            &:focus { border: none; }
            & > .option-list--option-highlighted {
                background: $block-cursor-blurred-background;
                color: $block-cursor-blurred-foreground;
                text-style: $block-cursor-blurred-text-style;
            }
            &:focus > .option-list--option-highlighted {
                background: $block-cursor-background;
                color: $block-cursor-foreground 100%;
                text-style: $block-cursor-text-style;
            }
            & > .option-list--option-hover { background: $block-hover-background; }
        }
        &:dark {
            .value--key { color: #98bfe5; }
            .value--string, .value-reader.string { color: #b4cfaa; }
            .value--number, .value-reader.number { color: #e5c18a; }
            .value--boolean, .value-reader.boolean { color: #c3afe5; }
            .value--muted, .value-status.muted, .value-reader.muted { color: #acbac8; }
        }
    }
    """

    def __init__(self) -> None:
        """Start closed, with no selected value."""
        super().__init__(classes="value-browser")
        self.root: BrowseRoot | None = None
        self._frames: list[_BrowseFrame] = []
        self._awaiting_value = False
        self._restore: _Bookmark | None = None
        self._maximized: _Bookmark | None = None
        self._revision = 0
        self.display = False
        self.identity = Static("", markup=False, classes="value-identity")
        self.crumbs = HorizontalScroll(
            self.identity, classes="value-path", can_focus=False
        )
        self._crumb_depths: dict[Button, int] = {}
        self.metadata = Static("", markup=False, classes="value-type")
        self.options = ValueChildren(classes="value-children")
        self.options.id = "value-children"
        self.reader = ScalarReader(classes="value-reader")
        self.reader.id = "value-scalar"
        self.status = Static("", markup=False)
        self.status_view = VerticalScroll(
            self.status, id="value-status", classes="value-status muted"
        )
        self._bodies = ContentSwitcher(
            self.status_view,
            self.options,
            self.reader,
            initial="value-status",
            classes="value-bodies",
        )
        self.clear(empty=False)

    def compose(self) -> ComposeResult:
        """Yield the selected identity, scalar reader and metadata."""
        yield Static("SELECTED VALUE", classes="value-heading")
        yield self.crumbs
        yield self.metadata
        yield self._bodies

    def select(self, root: BrowseRoot) -> None:
        """Start an explicit inspection and discard the previous browse path.

        Args:
            root: Selection in the current table generation. A missing value
                stays pending until the source pane delivers or fails this read.
        """
        self.clear(empty=False)
        self.root = root
        self.display = True
        if root.value is None:
            self._awaiting_value = True
            self.identity.update(root.label)
            self._show_status("Loading value…")
            return
        self._frames = [_BrowseFrame(root.label, ValueNode(root.value))]
        self._show_frame(focus=False)

    def resolve(self, root: BrowseRoot, value: pa.Scalar | RowValue) -> None:
        """Complete the pending root while preserving visibility and focus.

        Args:
            root: The original selection, matched by identity across late reads.
            value: The loaded cell or row.
        """
        if self.pending_root is not root:
            return
        self._awaiting_value = False
        self.root = replace(root, value=value)
        self._frames = [_BrowseFrame(root.label, ValueNode(value))]
        self._show_frame(focus=self.has_focus_within)

    def fail(self, root: BrowseRoot, message: str) -> None:
        """Keep a failed selection identifiable without presenting stale content."""
        if self.pending_root is not root:
            return
        self._awaiting_value = False
        self.status_view.remove_class("muted")
        self.status_view.add_class("error")
        self._show_status(f"Read error\n{message}")

    def toggle_visibility(self) -> None:
        """Resume the saved viewport after native controls handle being shown."""
        self.display = not self.display

    def on_hide(self) -> None:
        """Retain the viewport when an owning tab or viewing area is hidden."""
        self._save_position()

    @property
    def pending_root(self) -> BrowseRoot | None:
        """The selection still waiting for a source page."""
        return self.root if self._awaiting_value else None

    @property
    def _active_reader(self) -> ValueChildren | ScalarReader:
        return self.options if self._frames[-1].node.is_container else self.reader

    def _destination(self) -> ReadingPosition | None:
        bookmark = self._restore
        if (
            bookmark is not None
            and self._frames
            and bookmark.navigation_revision == self._active_reader.navigation_revision
        ):
            return bookmark.position
        return None

    def _reset_bookmarks(self) -> None:
        """Activate a browse frame, or clear the selection and its pending work."""
        self._revision += 1
        self._maximized = None
        self._restore = None
        if self._frames:
            frame = self._frames[-1]
            self._active_reader.restore_position(frame.position)
            self._restore = _Bookmark(
                frame.position, self._active_reader.navigation_revision
            )
            self._active_reader.call_after_refresh(self._restore_frame, self._revision)

    def _save_position(self) -> None:
        """Remember the intended position before leaving or hiding a frame."""
        self._revision += 1
        if self._frames:
            self._frames[-1].position = (
                self._destination() or self._active_reader.capture_position()
            )
        self._restore = None

    def on_show(self) -> None:
        """Reapply the saved position after Textual lays out the visible reader."""
        if self._frames:
            self._restore = _Bookmark(
                self._frames[-1].position, self._active_reader.navigation_revision
            )
        self.call_after_refresh(self._settle, self._revision)

    def set_maximized(self, maximized: bool) -> None:
        """Remember a position on maximize and restore it when still applicable.

        Args:
            maximized: Whether the table and its sidebar now fill the screen.
        """
        if not self._frames:
            return
        frame = self._frames[-1]
        if maximized:
            self._maximized = _Bookmark(
                self._destination() or self._active_reader.capture_position(),
                self._active_reader.navigation_revision,
            )
        elif self._maximized is not None:
            self._restore = self._maximized
            self._maximized = None
            if (position := self._destination()) is not None:
                frame.position = position
        self._revision += 1
        self.call_after_refresh(self._settle, self._revision)

    def _layout_changed(self) -> None:
        """Reflow after layout, ignoring callbacks superseded by a frame change."""
        self.call_after_refresh(self._reflow_current, self._revision)

    def _reflow_current(self, revision: int) -> None:
        if revision == self._revision and self.is_on_screen:
            self._reflow(self._destination())

    def _restore_frame(self, revision: int) -> None:
        if revision == self._revision and self.display:
            position = self._destination()
            if position is not None:
                self._active_reader.restore_position(position)
            self._restore = None

    def _settle(self, revision: int) -> None:
        if revision == self._revision and self.is_on_screen:
            self._reflow(self._destination())
            self._restore_frame(revision)

    def _show_frame(self, *, focus: bool) -> None:
        frame = self._frames[-1]
        self._show_breadcrumbs()
        self.metadata.update(frame.node.metadata)
        self.metadata.tooltip = Content(
            frame.node.metadata
            if frame.node.is_container
            else frame.node.type_description
        )
        self._bodies.current = self._active_reader.id
        if frame.node.is_container:
            self.reader.clear()
            self.options.show(
                frame.node,
                width=self._body_width(),
                selected=frame.selected,
                styles=self._preview_styles(),
            )
        else:
            self._show_scalar(frame.node.scalar)
        self._reset_bookmarks()
        if focus:
            self._active_reader.focus()

    def _show_breadcrumbs(self) -> None:
        self.identity.display = False
        self._crumb_depths.clear()
        self.crumbs.query(".value-crumb, .value-separator").remove()
        widgets: list[Widget] = []
        for depth, frame in enumerate(self._frames):
            if depth:
                widgets.append(Static(" > ", classes="value-separator"))
            label = _label_text(frame.label)
            label.truncate(27, overflow="ellipsis")
            button = Button(
                Content(label.plain),
                compact=True,
                tooltip=Content(frame.label),
                classes="value-crumb current"
                if depth == len(self._frames) - 1
                else "value-crumb",
            )
            button.can_focus = False
            self._crumb_depths[button] = depth
            widgets.append(button)
        self.crumbs.mount(*widgets)
        self.crumbs.call_after_refresh(
            lambda: self.crumbs.scroll_to(x=self.crumbs.max_scroll_x, animate=False)
        )

    @on(Button.Pressed, ".value-crumb")
    def _open_ancestor(self, event: Button.Pressed) -> None:
        event.stop()
        depth = self._crumb_depths.get(event.button)
        if depth is not None and depth < len(self._frames) - 1:
            self._frames = self._frames[: depth + 1]
            self._show_frame(focus=True)

    def _show_status(self, text: str) -> None:
        self.status.update(text)
        if self.status_view.is_attached:
            self._bodies.current = "value-status"
        self.status_view.scroll_home(animate=False, immediate=True)

    def _show_scalar(self, value: pa.Scalar) -> None:
        self.reader.show(value, width=self._scalar_width())
        self._update_scalar_metadata()

    @on(ScalarReader.Updated)
    def _scalar_updated(self, event: ScalarReader.Updated) -> None:
        event.stop()
        if (
            event.document is self.reader.document
            and self._frames
            and not self._frames[-1].node.is_container
        ):
            self._update_scalar_metadata()

    def _update_scalar_metadata(self) -> None:
        content = self.reader.document
        if content is None:
            return
        self.metadata.update(content.metadata)
        self.reader.remove_class("string", "number", "boolean", "muted")
        self.reader.add_class(content.role)

    def _preview_styles(self) -> dict[str, Style]:
        # Only foreground colors belong to the preview. ValueChildren owns the
        # selection and hover backgrounds.
        return {
            role: Style(color=self.get_component_rich_style(f"value--{role}").color)
            for role in ("key", "string", "number", "boolean", "muted")
        }

    def _body_width(self) -> int:
        # Reserve the scrollbar even before it appears, avoiding a wrapping cycle.
        # The browser keeps its width when the child list is hidden for a scalar.
        return max(20, self.content_size.width - 2)

    def _scalar_width(self) -> int:
        # The browser is measurable even when switching from a container to text.
        # Reserve the scrollbar so its appearance does not change wrapping.
        return max(1, self.content_size.width - self.reader.styles.gutter.width - 3)

    def on_resize(self) -> None:
        """Reflow previews and binary rows while retaining the reading position."""
        self._layout_changed()

    def notify_style_update(self) -> None:
        """Refresh explicit preview colors when the application theme changes."""
        super().notify_style_update()
        if self.is_mounted:
            self._layout_changed()

    def _reflow(self, position: ReadingPosition | None) -> None:
        if not self._frames:
            return
        frame = self._frames[-1]
        if frame.node.is_container:
            self.options.reflow(
                self._body_width(), styles=self._preview_styles(), position=position
            )
        else:
            self.reader.reflow(self._scalar_width())

    @on(ValueChildren.Selected)
    def _open_child(self, event: ValueChildren.Selected) -> None:
        event.stop()
        if (
            not self._frames
            or event.document is not self.options.document
            or event.document.node is not self._frames[-1].node
        ):
            return
        frame = self._frames[-1]
        frame.selected = event.index
        self._save_position()
        name, child = frame.node.child(frame.selected)
        self._frames.append(_BrowseFrame(name, child))
        self._show_frame(focus=True)

    def back(self) -> bool:
        """Restore the parent, returning false when already at the root."""
        if len(self._frames) < 2:
            return False
        self._frames.pop()
        self._show_frame(focus=True)
        return True

    def clear(self, *, empty: bool) -> None:
        """Release the selected root when its owning data is replaced or closed."""
        self.root = None
        self._frames.clear()
        self._awaiting_value = False
        self._reset_bookmarks()
        self._crumb_depths.clear()
        self.identity.display = True
        if self.crumbs.is_mounted:
            self.crumbs.query(".value-crumb, .value-separator").remove()
            self.crumbs.scroll_to(x=0, animate=False)
        had_focus = self.has_focus_within
        self.options.clear()
        self.reader.clear()
        self.identity.update("No selection")
        self.identity.tooltip = None
        self.metadata.update("")
        self.metadata.tooltip = None
        self.status_view.remove_class("error")
        self.status_view.add_class("muted")
        self._show_status(
            "No rows to inspect."
            if empty
            else "Press Enter in the table to select a cell or row."
        )
        self.options.tooltip = None
        if had_focus and self.is_mounted and self.display:
            self.status_view.focus()
