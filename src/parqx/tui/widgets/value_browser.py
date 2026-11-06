"""Read an explicitly selected Arrow value without using table previews."""

from dataclasses import dataclass, field, replace
from typing import ClassVar

import pyarrow as pa
from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import HorizontalScroll, Vertical
from textual.content import Content
from textual.widget import Widget
from textual.widgets import Button, Static

from parqx.tui.value_document import ReadingPosition
from parqx.tui.value_node import RowValue, ValueNode
from parqx.tui.widgets.scalar_reader import ScalarReader
from parqx.tui.widgets.value_children import ValueChildren


def _label_text(label: str) -> Text:
    """Keep field names on one visible line without interpreting markup."""
    return Text(label.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))


@dataclass(frozen=True)
class BrowseRoot:
    """A selected value tied to one owning view and one data generation."""

    owner: object
    generation: int
    row: int
    column: int | None
    name: str
    value: pa.Scalar | RowValue | None

    @property
    def label(self) -> str:
        """Identify the selected row and optional cell without needing its data."""
        label = f"Row {self.row}"
        return label if self.column is None else f"{label} · {self.name}"


@dataclass
class BrowseFrame:
    """A visited value and the reading position to restore on return."""

    label: str
    node: ValueNode
    selected: int = 0
    position: ReadingPosition = field(default_factory=ReadingPosition)


@dataclass
class _MaximizedPosition:
    """Compare the settled enlarged viewport with the user's later position."""

    frame: BrowseFrame
    position: ReadingPosition
    selected: int
    enlarged_scroll: float | None = None


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
        &:focus-within { border-left: solid $primary; }
        .value--key { color: #1d4b70; }
        .value--string, .value-reader.string { color: #1b5632; }
        .value--number, .value-reader.number { color: #713e0f; }
        .value--boolean, .value-reader.boolean { color: #583d7c; }
        .value--muted, .value-reader.muted { color: #454f5e; }
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
            color: $text-muted; border-bottom: solid $primary-muted;
        }
        .value-reader {
            height: 1fr; padding: 0 2; border: none; background: $surface;
            &:focus { border: none; }
            &.error { color: $error; }
        }
        .value-info {
            height: 3; border-top: solid $primary-muted;
            padding: 0 1; color: $text-muted;
        }
        .value-children {
            height: 1fr; padding: 0; border: none; background: $surface;
            &:focus { border: none; background-tint: transparent; }
            & > .option-list--option-highlighted {
                text-style: none; background: $block-cursor-blurred-background 25%;
            }
            &:focus > .option-list--option-highlighted {
                background: $block-cursor-background 15%;
            }
            & > .option-list--option-hover { background: $block-hover-background; }
        }
        &:dark {
            .value--key { color: #91b8de; }
            .value--string, .value-reader.string { color: #b4cfaa; }
            .value--number, .value-reader.number { color: #e5c18a; }
            .value--boolean, .value-reader.boolean { color: #bba7dd; }
            .value--muted, .value-reader.muted { color: #a3b1bf; }
        }
    }
    """

    def __init__(self) -> None:
        """Start closed, with no selected value."""
        super().__init__(classes="value-browser")
        self.root: BrowseRoot | None = None
        self._frames: list[BrowseFrame] = []
        self.display = False
        self.identity = Static("", markup=False, classes="value-identity")
        self.crumbs = HorizontalScroll(
            self.identity, classes="value-path", can_focus=False
        )
        self._crumb_depths: dict[Button, int] = {}
        self.metadata = Static("", markup=False, classes="value-type")
        self.info = Static("", markup=False, classes="value-info")
        self.options = ValueChildren(classes="value-children")
        self._render_width = 0
        self._render_styles: dict[str, Style] = {}
        self._reader_width = 0
        self._resume_frame: BrowseFrame | None = None
        self._maximized_position: _MaximizedPosition | None = None
        self.options.display = False
        self.reader = ScalarReader(classes="value-reader")
        self.clear(empty=False)

    def compose(self) -> ComposeResult:
        """Yield the selected identity, scalar reader and metadata."""
        yield Static("SELECTED VALUE", classes="value-heading")
        yield self.crumbs
        yield self.metadata
        yield self.options
        yield self.reader
        yield self.info

    def select(self, root: BrowseRoot) -> None:
        """Start an explicit inspection, including a value awaiting a source read."""
        self.clear(empty=False)
        self.root = root
        self.display = True
        if root.value is None:
            self.identity.update(root.label)
            self.reader.load_text("Loading value…")
            self.info.update("Reading source")
            return
        self._frames = [BrowseFrame(root.label, ValueNode(root.value))]
        self._show_frame(focus=False)

    def resolve(self, root: BrowseRoot, value: pa.Scalar | RowValue) -> None:
        """Complete only this root, preserving visibility and the user's focus."""
        if self.root is not root:
            return
        self.root = replace(root, value=value)
        self._frames = [BrowseFrame(root.label, ValueNode(value))]
        self._show_frame(focus=self.has_focus_within)

    def fail(self, root: BrowseRoot, message: str) -> None:
        """Keep a failed selection identifiable without presenting stale content."""
        if self.root is not root:
            return
        self.reader.remove_class("muted")
        self.reader.add_class("error")
        self.reader.load_text(f"Read error\n{message}")
        self.info.update("Read error")

    def toggle_visibility(self) -> None:
        """Resume the saved viewport after native controls handle being shown."""
        if self.display and self._frames:
            frame = self._frames[-1]
            body = self.options if frame.node.is_container else self.reader
            frame.position = body.capture_position()
        self.display = not self.display
        if self.display and self._frames:
            self.call_after_refresh(self._restore_position, self._frames[-1])

    def on_hide(self) -> None:
        """Retain the viewport when an owning tab or viewing area is hidden."""
        if self._frames:
            frame = self._frames[-1]
            body = self.options if frame.node.is_container else self.reader
            frame.position = body.capture_position()

    def on_show(self) -> None:
        """Restore after native controls scroll their highlighted item on show."""
        if self._frames:
            self._resume_frame = self._frames[-1]
            self.call_after_refresh(self._resume_viewport, self._frames[-1])
        else:
            self.call_after_refresh(self._reflow)

    def _resume_viewport(self, frame: BrowseFrame) -> None:
        if self._resume_frame is frame and self._frames and self._frames[-1] is frame:
            self._reflow()
            self._restore_position(frame)
            self._resume_frame = None

    def _restore_position(self, frame: BrowseFrame) -> None:
        if self.display and self._frames and self._frames[-1] is frame:
            body = self.options if frame.node.is_container else self.reader
            body.restore_position(frame.position)

    def begin_maximize(self) -> None:
        """Save the reading position before a larger viewport can clamp it."""
        if self._frames:
            frame = self._frames[-1]
            body = self.options if frame.node.is_container else self.reader
            position = _MaximizedPosition(
                frame, body.capture_position(), frame.selected
            )
            self._maximized_position = position
            self.call_after_refresh(self._reflow_maximized, position)

    def _has_position(self, position: _MaximizedPosition) -> bool:
        return (
            self._maximized_position is position
            and bool(self._frames)
            and self._frames[-1] is position.frame
            and self.is_attached
        )

    def _reflow_maximized(self, position: _MaximizedPosition) -> None:
        if self._has_position(position):
            self._reflow()
            body = self.options if position.frame.node.is_container else self.reader
            position.enlarged_scroll = body.scroll_y

    def restore_after_maximize(self) -> None:
        """Restore layout-induced movement while respecting newer user navigation."""
        position = self._maximized_position
        if position is None:
            return
        body = self.options if position.frame.node.is_container else self.reader
        if (
            self._has_position(position)
            and position.frame.selected == position.selected
            and body.scroll_y == position.enlarged_scroll
        ):
            self.call_after_refresh(self._reflow_restored, position)
        else:
            self._maximized_position = None

    def _reflow_restored(self, position: _MaximizedPosition) -> None:
        if self._has_position(position):
            self._reflow()
            position.frame.position = position.position
            self._restore_position(position.frame)
            self._maximized_position = None

    def _show_frame(self, *, focus: bool) -> None:
        self._resume_frame = None
        self._maximized_position = None
        frame = self._frames[-1]
        self._show_breadcrumbs()
        self.metadata.update(frame.node.metadata)
        self.metadata.tooltip = Content(
            frame.node.metadata
            if frame.node.is_container
            else frame.node.type_description
        )
        self.options.display = frame.node.is_container
        self.reader.display = not frame.node.is_container
        if frame.node.is_container:
            self.reader.load_text("")
            self._show_children(frame)
        else:
            self._show_scalar(frame.node.scalar)
        body = self.options if frame.node.is_container else self.reader
        body.restore_position(frame.position)
        body.call_after_refresh(self._restore_position, frame)
        if focus:
            body.focus()

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

    def _show_scalar(self, value: pa.Scalar) -> None:
        self._reader_width = self._scalar_width()
        self.reader.show(value, width=self._reader_width)
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
            position = self._maximized_position
            if (
                event.restoring
                and position is not None
                and self._has_position(position)
            ):
                position.enlarged_scroll = self.reader.scroll_y

    def _update_scalar_metadata(self) -> None:
        content = self.reader.document
        if content is None:
            return
        self.metadata.update(content.metadata)
        self.reader.remove_class("string", "number", "boolean", "muted", "error")
        self.reader.add_class(content.role)
        self.info.update(
            content.info if content.complete else "Preparing text…\nRead only"
        )
        self.info.tooltip = None

    def _preview_styles(self) -> dict[str, Style]:
        # Only foreground colors belong to the preview. ValueChildren owns the
        # background, including its full-width selection and hover states.
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
        self.call_after_refresh(self._reflow)

    def notify_style_update(self) -> None:
        """Refresh explicit preview colors when the application theme changes."""
        super().notify_style_update()
        if self.is_mounted:
            self.call_after_refresh(self._reflow)

    def _reflow(self) -> None:
        if not self.is_on_screen:
            return
        if not self._frames:
            self.reader.reflow(self._scalar_width())
            return
        frame = self._frames[-1]
        if frame.node.is_container:
            if (
                self._body_width() != self._render_width
                or self._preview_styles() != self._render_styles
            ):
                if self._resume_frame is not frame:
                    frame.position = self.options.capture_position()
                self._show_children(frame)
                self._restore_position(frame)
        elif self._scalar_width() != self._reader_width:
            self._reader_width = self._scalar_width()
            self.reader.reflow(self._reader_width)

    def _show_children(self, frame: BrowseFrame) -> None:
        self._render_width = self._body_width()
        self._render_styles = self._preview_styles()
        self.options.show(
            frame.node,
            width=self._render_width,
            selected=frame.selected,
            styles=self._render_styles,
        )
        if frame.node.child_count:
            self._selection_info(frame)
        else:
            self.info.update("Empty container\n0 children")
            self.info.tooltip = None

    def _selection_info(self, frame: BrowseFrame) -> None:
        name, child = frame.node.child(frame.selected)
        width = max(1, self.content_size.width - 2)
        label = _label_text(name)
        label.truncate(width, overflow="ellipsis")
        metadata = Text(child.metadata)
        metadata.truncate(width, overflow="ellipsis")
        self.info.update(label + Text("\n") + metadata)
        self.info.tooltip = Content(
            f"{name}\n{child.metadata if child.is_container else child.type_description}\n{child.metadata}"
        )

    def _frame_for_option(
        self, event: ValueChildren.ChildMessage
    ) -> BrowseFrame | None:
        """Accept only children from the current rendered container."""
        if (
            self._frames
            and self.options.document is event.document
            and self._frames[-1].node is event.document.node
        ):
            return self._frames[-1]
        return None

    @on(ValueChildren.Highlighted)
    def _highlighted(self, event: ValueChildren.Highlighted) -> None:
        event.stop()
        frame = self._frame_for_option(event)
        if frame is not None:
            frame.selected = event.index
            self._selection_info(frame)

    @on(ValueChildren.Selected)
    def _open_child(self, event: ValueChildren.Selected) -> None:
        event.stop()
        frame = self._frame_for_option(event)
        if frame is None:
            return
        frame.selected = event.index
        frame.position = self.options.capture_position()
        name, child = frame.node.child(frame.selected)
        self._frames.append(BrowseFrame(name, child))
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
        self._resume_frame = None
        self._maximized_position = None
        self._crumb_depths.clear()
        self.identity.display = True
        if self.crumbs.is_mounted:
            self.crumbs.query(".value-crumb, .value-separator").remove()
            self.crumbs.scroll_to(x=0, animate=False)
        had_focus = self.options.has_focus
        self.options.display = False
        self.options.clear_options()
        self.reader.display = True
        self.identity.update("No selection")
        self.identity.tooltip = None
        self.metadata.update("")
        self.metadata.tooltip = None
        self._reader_width = 0
        self.reader.remove_class("string", "number", "boolean", "error")
        self.reader.add_class("muted")
        self.reader.load_text(
            "No rows to inspect."
            if empty
            else "Press Enter in the table to select a cell or row."
        )
        self.reader.scroll_to(x=0, y=0, animate=False, force=True)
        self.info.update("")
        self.info.tooltip = None
        self.options.tooltip = None
        if had_focus and self.is_mounted and self.display:
            self.reader.focus()
