"""Read an explicitly selected Arrow value without using table previews."""

from dataclasses import replace
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

from parqx.tui.values.children import ValueChildren
from parqx.tui.values.document import ReadingPosition
from parqx.tui.values.node import RowValue, ValueNode
from parqx.tui.values.reader import ScalarReader
from parqx.tui.values.selection import BrowseRoot
from parqx.tui.values.viewport import BrowseFrame, BrowseViewport


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
        .value--muted, .value-reader.muted { color: #3e4856; }
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
        .value-reader {
            height: 1fr; padding: 0 2; border: none; background: transparent;
            &:focus { border: none; }
            &.error { color: $error; }
        }
        .value-info {
            height: 3; border-top: solid $foreground 20%;
            padding: 0 1; color: $text-muted;
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
            .value--muted, .value-reader.muted { color: #acbac8; }
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
        self.options.display = False
        self.reader = ScalarReader(classes="value-reader")
        self.viewport = BrowseViewport(self, self.options, self.reader, self._reflow)
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
            self._show_status("Loading value…")
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
        self._show_status(f"Read error\n{message}")
        self.info.update("Read error")

    def toggle_visibility(self) -> None:
        """Resume the saved viewport after native controls handle being shown."""
        if self.display:
            self.viewport.save()
        self.display = not self.display

    def on_hide(self) -> None:
        """Retain the viewport when an owning tab or viewing area is hidden."""
        self.viewport.save()

    def on_show(self) -> None:
        """Restore after Textual lays out the visible sidebar."""
        self.viewport.resume()

    def _show_frame(self, *, focus: bool) -> None:
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
            self._show_status("")
            self._show_children(frame)
        else:
            self._show_scalar(frame.node.scalar)
        self.viewport.show(frame)
        if focus:
            self.viewport.body.focus()

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
        self.reader.show(pa.scalar(text), width=self._scalar_width())

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
            if event.restored_scroll is not None:
                self.viewport.scalar_restored(event.restored_scroll)

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
        self.viewport.layout_changed()

    def notify_style_update(self) -> None:
        """Refresh explicit preview colors when the application theme changes."""
        super().notify_style_update()
        if self.is_mounted:
            self.viewport.layout_changed()

    def _reflow(self, position: ReadingPosition | None) -> None:
        if not self._frames:
            self.reader.reflow(self._scalar_width())
            return
        frame = self._frames[-1]
        if frame.node.is_container:
            rewrapped = self.options.reflow(
                self._body_width(), styles=self._preview_styles(), position=position
            )
            if rewrapped and self.options.highlighted is not None:
                frame.selected = self.options.highlighted
                self._selection_info(frame)
        else:
            self.reader.reflow(self._scalar_width())

    def _show_children(self, frame: BrowseFrame) -> None:
        self.options.show(
            frame.node,
            width=self._body_width(),
            selected=frame.selected,
            styles=self._preview_styles(),
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

    def _frame_for_child(self, event: ValueChildren.ChildMessage) -> BrowseFrame | None:
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
        frame = self._frame_for_child(event)
        if frame is not None:
            frame.selected = event.index
            self._selection_info(frame)

    @on(ValueChildren.Selected)
    def _open_child(self, event: ValueChildren.Selected) -> None:
        event.stop()
        frame = self._frame_for_child(event)
        if frame is None:
            return
        frame.selected = event.index
        self.viewport.save()
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
        self.viewport.show(None)
        self._crumb_depths.clear()
        self.identity.display = True
        if self.crumbs.is_mounted:
            self.crumbs.query(".value-crumb, .value-separator").remove()
            self.crumbs.scroll_to(x=0, animate=False)
        had_focus = self.options.has_focus
        self.options.display = False
        self.options.clear()
        self.reader.display = True
        self.identity.update("No selection")
        self.identity.tooltip = None
        self.metadata.update("")
        self.metadata.tooltip = None
        self.reader.remove_class("string", "number", "boolean", "error")
        self.reader.add_class("muted")
        self._show_status(
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
