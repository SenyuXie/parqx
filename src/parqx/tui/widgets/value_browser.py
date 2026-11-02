"""Read an explicitly selected Arrow value without using table previews."""

from dataclasses import dataclass

import pyarrow as pa
from textual.app import ComposeResult
from textual.containers import HorizontalScroll, Vertical
from textual.content import Content
from textual.widgets import Static, TextArea

from parqx.tui.scalar_value import read_scalar


@dataclass(frozen=True)
class BrowseRoot:
    """A selected value tied to one owning view and one data generation."""

    owner: object
    generation: int
    row: int
    column: int
    name: str
    value: pa.Scalar


class ValueBrowser(Vertical):
    """Keep selected content and its reading position while hidden."""

    DEFAULT_CSS = """
    ValueBrowser {
        width: 36%; height: 1fr;
        border-left: solid $primary-muted;
        padding: 1 1 0 1; background: $surface;
        &:focus-within { border-left: solid $accent; }
        .value-heading {
            height: 1; padding: 0 1; color: $text-muted; text-style: bold;
        }
        .value-path {
            height: 2; margin-top: 1; padding: 0 1;
            scrollbar-size-horizontal: 1;
        }
        .value-identity { width: auto; height: 1; text-style: bold; }
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
        &:dark {
            background: #19232e; border-left: solid #344859;
            &:focus-within { border-left: solid #86bfc1; }
            .value-heading { color: #7c91a6; }
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
        }
    }
    """

    def __init__(self) -> None:
        """Start closed, with no selected value."""
        super().__init__(classes="value-browser")
        self.root: BrowseRoot | None = None
        self.display = False
        self.identity = Static("", markup=False, classes="value-identity")
        self.metadata = Static("", markup=False, classes="value-type")
        self.info = Static("", markup=False, classes="value-info")
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
        with HorizontalScroll(classes="value-path", can_focus=False):
            yield self.identity
        yield self.metadata
        yield self.reader
        yield self.info

    def select(self, root: BrowseRoot) -> None:
        """Start a fresh inspection of a complete loaded scalar."""
        self.root = root
        label = f"Row {root.row} · {root.name}"
        self.identity.update(label)
        self.identity.tooltip = Content(label)
        content = read_scalar(root.value)
        self.metadata.update(content.metadata)
        self.metadata.tooltip = Content(content.metadata)
        self.reader.remove_class("string", "number", "boolean", "muted")
        self.reader.add_class(content.role)
        self.reader.load_text(content.text)
        self.reader.scroll_to(x=0, y=0, animate=False, force=True)
        self.info.update(content.info)
        self.display = True

    def clear(self, *, empty: bool) -> None:
        """Release the selected root when its owning data is replaced or closed."""
        self.root = None
        self.identity.update("No selection")
        self.identity.tooltip = None
        self.metadata.update("")
        self.metadata.tooltip = None
        self.reader.remove_class("string", "number", "boolean")
        self.reader.add_class("muted")
        self.reader.load_text(
            "No rows to inspect."
            if empty
            else "Press Enter in the table to select a cell or row."
        )
        self.reader.scroll_to(x=0, y=0, animate=False, force=True)
        self.info.update("")

    def show_unsupported_row(self, row: int, field_count: int) -> None:
        """Identify a row selection until row exploration is available."""
        self.clear(empty=False)
        self.identity.update(f"Row {row}")
        self.metadata.update(f"row · struct · {field_count:,} fields")
        self.reader.load_text("Row browsing is not supported yet.")
        self.info.update("Unsupported value")
        self.display = True
