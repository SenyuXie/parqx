"""Read an explicitly selected Arrow value without using table previews."""

from dataclasses import dataclass
from typing import ClassVar

import pyarrow as pa
from rich.style import Style
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.containers import HorizontalScroll, Vertical
from textual.content import Content
from textual.widget import Widget
from textual.widgets import Button, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from parqx.tui.scalar_value import read_scalar
from parqx.tui.value_node import RowValue, ValueNode


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
    value: pa.Scalar | RowValue


@dataclass
class BrowseFrame:
    """A visited value and the reading position to restore on return."""

    label: str
    node: ValueNode
    selected: int = 0
    scroll: float = 0


class ValueChildren(OptionList):
    """Expose full field identities through tooltips on styled child prompts."""

    def on_mouse_move(self, event: events.MouseMove) -> None:
        """Read prompt metadata without depending on OptionList's line cache."""
        label = event.style.meta.get("value_tooltip")
        self.tooltip = Content(label) if isinstance(label, str) else None

    def on_leave(self) -> None:
        """Discard the tooltip after leaving the content."""
        self.tooltip = None


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
        .value--key { color: #245a86; }
        .value--string { color: #246d40; }
        .value--number { color: #925112; }
        .value--boolean { color: #694c91; }
        .value--muted { color: #536374; }
        .value-heading {
            height: 1; padding: 0 1; color: $text-muted; text-style: bold;
        }
        .value-path {
            height: 2; margin-top: 1; padding: 0 1;
            scrollbar-size-horizontal: 1;
            scrollbar-color: $primary-muted; scrollbar-background: $surface;
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
            &.string { color: $success; }
            &.number { color: $warning; }
            &.boolean { color: $secondary; }
            &.muted { color: $text-muted; }
        }
        .value-info {
            height: 3; border-top: solid $primary-muted;
            padding: 0 1; color: $text-muted;
        }
        .value-children {
            height: 1fr; padding: 0; border: none; background: $surface;
            &:focus { border: none; background-tint: transparent; }
            & > .option-list--option { padding: 0; }
            & > .option-list--option-highlighted {
                text-style: none; background: $primary 15%;
            }
            &:focus > .option-list--option-highlighted { background: $primary 25%; }
            & > .option-list--option-hover { background: $primary 10%; }
        }
        &:dark {
            background: #19232e;
            .value--key { color: #91b8de; }
            .value--string { color: #b4cfaa; }
            .value--number { color: #e5c18a; }
            .value--boolean { color: #bba7dd; }
            .value--muted { color: #8493a5; }
            .value-heading { color: #7c91a6; }
            .value-path { scrollbar-color: #465c70; scrollbar-background: #19232e; }
            .value-type, .value-info { color: #91a2b5; border: none; }
            .value-type { border-bottom: solid #2c3a49; }
            .value-info { border-top: solid #2c3a49; }
            .value-reader {
                background: #19232e;
                scrollbar-color: #465c70; scrollbar-background: #19232e;
                &.string { color: #b4cfaa; }
                &.number { color: #e5c18a; }
                &.boolean { color: #bba7dd; }
                &.muted { color: #8493a5; }
            }
            .value-children {
                background: #19232e;
                scrollbar-color: #465c70; scrollbar-background: #19232e;
                & > .option-list--option-highlighted { background: #21313f; }
                &:focus > .option-list--option-highlighted { background: #293e52; }
                & > .option-list--option-hover { background: #223243; }
            }
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
        self._binary_columns: int | None = None
        self.options.display = False
        self.reader = TextArea(
            read_only=True,
            soft_wrap=True,
            show_line_numbers=False,
            highlight_cursor_line=False,
            classes="value-reader",
        )
        self.reader.show_cursor = False
        self.reader.match_cursor_bracket = False
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
        """Start a fresh inspection of one available cell or row value."""
        self.root = root
        label = f"Row {root.row}"
        if root.column is not None:
            label += f" · {root.name}"
        self._frames = [BrowseFrame(label, ValueNode(root.value))]
        self.display = True
        self._show_frame(focus=False)

    def toggle_visibility(self) -> None:
        """Resume the saved viewport after native controls handle being shown."""
        if self.display and self._frames:
            frame = self._frames[-1]
            body = self.options if frame.node.is_container else self.reader
            frame.scroll = body.scroll_y
        self.display = not self.display
        if self.display and self._frames:
            self.call_after_refresh(self._restore_position, self._frames[-1])

    def _restore_position(self, frame: BrowseFrame) -> None:
        if self.display and self._frames and self._frames[-1] is frame:
            body = self.options if frame.node.is_container else self.reader
            body.scroll_to(y=frame.scroll, animate=False, force=True)

    def _show_frame(self, *, focus: bool) -> None:
        frame = self._frames[-1]
        self._show_breadcrumbs()
        self.metadata.update(frame.node.metadata)
        self.metadata.tooltip = Content(frame.node.type_description)
        self.options.display = frame.node.is_container
        self.reader.display = not frame.node.is_container
        if frame.node.is_container:
            self._show_children(frame)
        else:
            self._show_scalar(frame.node.scalar)
        body = self.options if frame.node.is_container else self.reader
        body.scroll_to(y=frame.scroll, animate=False, force=True)
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
        content = read_scalar(value, width=self._reader_width)
        self._binary_columns = content.bytes_per_line
        self.metadata.update(content.metadata)
        self.metadata.tooltip = Content(str(value.type))
        self.reader.remove_class("string", "number", "boolean", "muted")
        self.reader.add_class(content.role)
        self.reader.soft_wrap = content.bytes_per_line is None
        self.reader.load_text(content.text)
        self.reader.scroll_to(x=0, y=0, animate=False, force=True)
        self.info.update(content.info)
        self.info.tooltip = None

    def _child_prompt(self, frame: BrowseFrame, index: int) -> Text:
        return frame.node.child_prompt(
            index,
            width=self._render_width,
            selected=index == frame.selected,
            styles=self._render_styles,
        )

    def _preview_styles(self) -> dict[str, Style]:
        # Only foreground colors belong to the preview. OptionList owns the
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
        # Reserve the scrollbar and TextArea's extra end-of-line cell as well.
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
        if not (self.display and self.is_mounted and self._frames):
            return
        frame = self._frames[-1]
        if frame.node.is_container:
            if (
                self._body_width() != self._render_width
                or self._preview_styles() != self._render_styles
            ):
                frame.scroll = self.options.scroll_y
                self._show_children(frame)
                self.options.scroll_to(y=frame.scroll, animate=False, force=True)
        elif (
            self._binary_columns is not None
            and self._scalar_width() != self._reader_width
        ):
            byte_offset = int(self.reader.scroll_y) * self._binary_columns
            self._show_scalar(frame.node.scalar)
            frame.scroll = byte_offset // self._binary_columns
            self.reader.call_after_refresh(self._restore_position, frame)

    def _show_children(self, frame: BrowseFrame) -> None:
        self._render_width = self._body_width()
        self._render_styles = self._preview_styles()
        self.options.tooltip = None
        with self.options.prevent(OptionList.OptionHighlighted):
            self.options.clear_options()
            left, right = frame.node.delimiters
            if frame.node.child_count:
                self.options.add_options(
                    [Option(Text("  " + left), disabled=True)]
                    + [
                        Option(self._child_prompt(frame, i))
                        for i in range(frame.node.child_count)
                    ]
                    + [Option(Text("  " + right), disabled=True)]
                )
                self.options.highlighted = frame.selected + 1
                self._selection_info(frame)
            else:
                self.options.add_option(
                    Option(Text("  " + left + right), disabled=True)
                )
                self.options.highlighted = None
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
            f"{name}\n{child.type_description}\n{child.metadata}"
        )

    def _frame_for_option(self, event: OptionList.OptionMessage) -> BrowseFrame | None:
        """Accept only children belonging to the currently rendered container."""
        if (
            event.option_list is not self.options
            or not self._frames
            or not self._frames[-1].node.is_container
            or not 0 < event.option_index < self.options.option_count - 1
        ):
            return None
        if event.option is not self.options.get_option_at_index(event.option_index):
            return None
        return self._frames[-1]

    @on(OptionList.OptionHighlighted)
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        frame = self._frame_for_option(event)
        if frame is None:
            return
        previous = frame.selected
        frame.selected = event.option_index - 1
        for index in {previous, frame.selected}:
            self.options.replace_option_prompt_at_index(
                index + 1, self._child_prompt(frame, index)
            )
        self._selection_info(frame)

    @on(OptionList.OptionSelected)
    def _open_child(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        frame = self._frame_for_option(event)
        if frame is None:
            return
        frame.selected = event.option_index - 1
        frame.scroll = self.options.scroll_y
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
        self._binary_columns = None
        self._reader_width = 0
        self.reader.soft_wrap = True
        self.reader.remove_class("string", "number", "boolean")
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
