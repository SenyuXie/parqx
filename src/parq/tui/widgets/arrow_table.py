"""The ArrowTable widget."""

import random
from dataclasses import dataclass
from itertools import accumulate
from math import ceil
from typing import ClassVar, Literal, cast

import pyarrow as pa
from rich.console import RenderableType
from rich.filesize import decimal
from rich.padding import Padding
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding, BindingType
from textual.coordinate import Coordinate
from textual.geometry import Region, Size
from textual.message import Message
from textual.reactive import Reactive
from textual.render import measure
from textual.renderables.styled import Styled
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widget import PseudoClasses

CursorType = Literal["cell", "row", "column", "none"]

_BINARY_INLINE_LIMIT = 16
_COLUMN_WIDTH_PERCENTILE = 0.95


def format_cell(scalar: pa.Scalar) -> RenderableType:
    """Convert a cell into a Rich renderable for display.

    Args:
        scalar: Arrow scalar for a cell.

    Returns:
        A single-line renderable representing the data.
    """
    if not scalar.is_valid:
        return Text("null", style="dim italic magenta")

    data_type, value = scalar.type, scalar.as_py()

    if pa.types.is_boolean(data_type):
        return Text("true", style="green") if value else Text("false", style="red")

    if (
        pa.types.is_binary(data_type)
        or pa.types.is_large_binary(data_type)
        or pa.types.is_fixed_size_binary(data_type)
    ):
        if len(value) <= _BINARY_INLINE_LIMIT:
            return Text("0x" + value.hex(), style="dim cyan")
        return Text(f"<binary {decimal(len(value))}>", style="dim cyan")

    if (
        pa.types.is_list(data_type)
        or pa.types.is_large_list(data_type)
        or pa.types.is_fixed_size_list(data_type)
    ):
        return Text(f"<list {len(value)}>", style="blue")

    if pa.types.is_struct(data_type):
        return Text(f"<struct {len(value)} fields>", style="blue")

    if pa.types.is_map(data_type):
        return Text(f"<map {len(value)}>", style="blue")

    if (
        pa.types.is_integer(data_type)
        or pa.types.is_floating(data_type)
        or pa.types.is_decimal(data_type)
    ):
        return Text(str(value), style="cyan")

    if pa.types.is_temporal(data_type):
        return Text(str(value), style="yellow")

    return Text(
        str(value)
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


@dataclass
class ArrowColumn:
    """Metadata for a column in the ArrowTable."""

    name: str
    """Column name from the Arrow schema."""
    content_width: int
    """Estimated p95 terminal-cell width over sampled, formatted cell values."""
    min_width: int = 4
    """Minimum content width, excluding horizontal padding."""
    max_width: int = 32
    """Maximum content width, excluding horizontal padding."""

    def get_render_width(self, padding: int = 1) -> int:
        """Width, in cells, required to render the column with padding included.

        Args:
            padding: Horizontal padding, applied on each side of each cell.

        Returns:
            The width, in cells, required to render the column with padding included.
        """
        width = max(len(self.name), self.content_width, self.min_width)
        return min(width, self.max_width) + padding * 2


def _sample_row_indices(
    total_rows: int,
    target_count: int = 2048,
    head_count: int = 128,
    tail_count: int = 128,
    random_count: int = 128,
    seed: int = 42,
) -> tuple[int, ...]:
    """Return row indices sampled from head, tail, evenly spaced, and random rows.

    Assumes `target_count >> head_count + tail_count + random_count`
    so the evenly spaced middle sample keeps most of the budget.

    Args:
        total_rows: Total number of rows available for sampling.
        target_count: Target number of sampled rows.
        head_count: The number of rows to include from the beginning.
        tail_count: The number of rows to include from the end.
        random_count: The number of random rows to sample from the middle.
        seed: Seed used for deterministic random sampling.

    Returns:
        Sorted, deduplicated row indices. The returned tuple may contain fewer
        than `target_count` rows when sampling strategies overlap.
    """
    if total_rows <= target_count:
        return tuple(range(total_rows))

    even_count = target_count - head_count - tail_count - random_count
    middle_start, middle_stop = head_count, total_rows - tail_count
    middle_count = middle_stop - middle_start

    indices: set[int] = set(range(middle_start))
    indices.update(range(middle_stop, total_rows))

    span = middle_count - 1
    denominator = max(even_count - 1, 1)
    indices.update(
        middle_start + round(i * span / denominator) for i in range(even_count)
    )

    rng = random.Random(seed)  # noqa: S311
    k = min(random_count, middle_count)
    indices.update(rng.sample(range(middle_start, middle_stop), k))

    return tuple(sorted(indices))


class ArrowTable(ScrollView, can_focus=True):
    """Arrow-backed data table widget."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "select_cursor", "Select", show=False),
        Binding("up", "cursor_up", "Cursor up", show=False),
        Binding("down", "cursor_down", "Cursor down", show=False),
        Binding("right", "cursor_right", "Cursor right", show=False),
        Binding("left", "cursor_left", "Cursor left", show=False),
        Binding("pageup", "page_up", "Page up", show=False),
        Binding("pagedown", "page_down", "Page down", show=False),
    ]
    """ArrowTable bindings:
    | Key      | Description                    |
    | :---     | :---                           |
    | enter    | Select cells under the cursor. |
    | up       | Move the cursor up.            |
    | down     | Move the cursor down.          |
    | right    | Move the cursor right.         |
    | left     | Move the cursor left.          |
    | pageup   | Move one page up.              |
    | pagedown | Move one page down.            |
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "arrowtable--cursor",
        "arrowtable--hover",
        "arrowtable--header",
        "arrowtable--header-cursor",
        "arrowtable--header-hover",
        "arrowtable--odd-row",
        "arrowtable--even-row",
    }
    """ArrowTable component classes:
    | Class                       | Description                                                 |
    | :---                        | :---                                                        |
    | `arrowtable--cursor`        | Target the cursor.                                          |
    | `arrowtable--hover`         | Target the cells under the hover cursor.                    |
    | `arrowtable--header`        | Target the header of the data table.                        |
    | `arrowtable--header-cursor` | Target cells highlighted by the cursor.                     |
    | `arrowtable--header-hover`  | Target hovered header or row index cells.                   |
    | `arrowtable--even-row`      | Target even rows (row indices start at 0) if zebra_stripes. |
    | `arrowtable--odd-row`       | Target odd rows (row indices start at 0) if zebra_stripes.  |
    """

    DEFAULT_CSS = """
    ArrowTable {
        background: $surface;
        color: $foreground;
        height: auto;
        max-height: 100%;

        &:focus {
            background-tint: $foreground 5%;
            & > .arrowtable--cursor {
                background: $block-cursor-background;
                color: $block-cursor-foreground;
                text-style: $block-cursor-text-style;
            }

            & > .arrowtable--header {
                background-tint: $foreground 5%;
            }
        }

        &:dark {
            & > .arrowtable--even-row {
                background: $surface-darken-1 40%;
            }
        }

        & > .arrowtable--header {
            text-style: bold;
            background: $panel;
            color: $foreground;
        }

        &:ansi > .arrowtable--header {
            background: ansi_bright_blue;
            color: ansi_default;
        }

        & > .arrowtable--odd-row {

        }

        & > .arrowtable--even-row {
            background: $surface-lighten-1 50%;
        }

        & > .arrowtable--cursor {
            background: $block-cursor-blurred-background;
            color: $block-cursor-blurred-foreground;
            text-style: $block-cursor-blurred-text-style;
        }

        & > .arrowtable--header-cursor {
            background: $accent-darken-1;
            color: $foreground;
        }

        & > .arrowtable--header-hover {
            background: $accent 30%;
        }

        & > .arrowtable--hover {
            background: $block-hover-background;
        }
    }
    """

    show_header = Reactive(True)
    """Show/hide the header row (the row of column labels)."""
    show_row_index = Reactive(True)
    """Show/hide the row index column containing zero-based row numbers."""
    zebra_stripes = Reactive(False)
    """Apply alternating styles, arrowtable--even-row and arrowtable--odd-row, to create a zebra effect."""
    header_height = Reactive(1)
    """The height of the header row (the row of column labels)."""
    show_cursor = Reactive(True)
    """Show/hide both the keyboard and hover cursor."""
    cursor_type: Reactive[CursorType] = Reactive[CursorType]("cell")
    """The type of the cursor of the `ArrowTable`."""
    cell_padding = Reactive(1)
    """Horizontal padding between cells, applied on each side of each cell."""

    cursor_coordinate: Reactive[Coordinate] = Reactive(
        Coordinate(0, 0), repaint=False, always_update=True
    )

    hover_coordinate: Reactive[Coordinate] = Reactive(
        Coordinate(0, 0), repaint=False, always_update=True
    )
    """The coordinate of the `ArrowTable` that is being hovered."""

    class CellHighlighted(Message):
        """Posted when the cursor moves to highlight a new cell.

        This is only relevant when the `cursor_type` is `"cell"`.
        It's also posted when the cell cursor is
        re-enabled (by setting `show_cursor=True`), and when the cursor type is
        changed to `"cell"`. Can be handled using `on_arrow_table_cell_highlighted` in
        a subclass of `ArrowTable` or in a parent widget in the DOM.
        """

    class CellSelected(Message):
        """Posted by the `ArrowTable` widget when a cell is selected.

        This is only relevant when the `cursor_type` is `"cell"`. Can be handled using
        `on_arrow_table_cell_selected` in a subclass of `ArrowTable` or in a parent
        widget in the DOM.
        """

    class RowHighlighted(Message):
        """Posted when a row is highlighted.

        This message is only posted when the
        `cursor_type` is set to `"row"`. Can be handled using
        `on_arrow_table_row_highlighted` in a subclass of `ArrowTable` or in a parent
        widget in the DOM.
        """

    class RowSelected(Message):
        """Posted when a row is selected.

        This message is only posted when the
        `cursor_type` is set to `"row"`. Can be handled using
        `on_arrow_table_row_selected` in a subclass of `ArrowTable` or in a parent
        widget in the DOM.
        """

    class ColumnHighlighted(Message):
        """Posted when a column is highlighted.

        This message is only posted when the
        `cursor_type` is set to `"column"`. Can be handled using
        `on_arrow_table_column_highlighted` in a subclass of `ArrowTable` or in a parent
        widget in the DOM.
        """

    class ColumnSelected(Message):
        """Posted when a column is selected.

        This message is only posted when the
        `cursor_type` is set to `"column"`. Can be handled using
        `on_arrow_table_column_selected` in a subclass of `ArrowTable` or in a parent
        widget in the DOM.
        """

    class HeaderSelected(Message):
        """Posted when a column header/label is clicked."""

    class RowIndexSelected(Message):
        """Posted when a row index cell is clicked."""

    def __init__(
        self,
        table: pa.Table,
        show_header: bool = True,
        show_row_index: bool = True,
        zebra_stripes: bool = False,
        header_height: int = 1,
        show_cursor: bool = True,
        cursor_foreground_priority: Literal["renderable", "css"] = "css",
        cursor_background_priority: Literal["renderable", "css"] = "renderable",
        cursor_type: CursorType = "cell",
        cell_padding: int = 1,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        """Initialize a widget to display Arrow-backed tabular data.

        Args:
            table: Arrow table used as the backing data source.
            show_header: Whether the table header should be visible or not.
            show_row_index: Whether zero-based row numbers should be shown or not.
            zebra_stripes: Enables or disables a zebra effect applied to the background
                color of the rows of the table, where alternate colors are styled
                differently to improve the readability of the table.
            header_height: The height, in number of cells, of the data table header.
            show_cursor: Whether the cursor should be visible when navigating the data
                table or not.
            cursor_foreground_priority: If the data associated with a cell is an
                arbitrary renderable with a set foreground color, this determines whether
                that color is prioritized over the cursor component class or not.
            cursor_background_priority: If the data associated with a cell is an
                arbitrary renderable with a set background color, this determines whether
                that color is prioritized over the cursor component class or not.
            cursor_type: The type of cursor to be used when navigating the data table
                with the keyboard.
            cell_padding: The number of cells added on each side of each column. Setting
                this value to zero will likely make your table very hard to read.
            name: The name of the widget.
            id: The ID of the widget in the DOM.
            classes: The CSS classes for the widget.
            disabled: Whether the widget is disabled or not.
        """
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)

        self._table = table
        """Arrow table used as the backing data source."""
        self._columns: tuple[ArrowColumn, ...] = ()
        """Column metadata in source column order."""
        self._column_widths: tuple[int, ...] = ()
        """Rendered column widths, including horizontal padding."""
        self._column_offsets: tuple[int, ...] = ()
        """Starting x offsets (including the row index) for rendered Arrow columns."""

        self._pseudo_class_state = PseudoClasses(False, False, False)
        """The pseudo-class state is used as part of cache keys to ensure that, for example,
        when we lose focus on the ArrowTable, rules which apply to :focus are invalidated
        and we prevent lingering styles."""

        self._require_update_dimensions = True
        """Set to re-calculate dimensions on idle."""

        self._show_hover_cursor = False
        """Used to hide the mouse hover cursor when the user uses the keyboard."""
        self._index_column_width = 0
        """Rendered row index column width, including horizontal padding."""

        self.show_header = show_header
        """Show/hide the header row (the row of column labels)."""
        self.show_row_index = show_row_index
        """Show/hide the row index column containing zero-based row numbers."""
        self.header_height = header_height
        """The height of the header row (the row of column labels)."""
        self.zebra_stripes = zebra_stripes
        """Apply alternating styles, arrowtable--even-row and arrowtable--odd-row, to create a zebra effect."""
        self.show_cursor = show_cursor
        """Show/hide both the keyboard and hover cursor."""
        self.cursor_foreground_priority = cursor_foreground_priority
        """Should we prioritize the cursor component class CSS foreground or the renderable foreground
        in the event where a cell contains a renderable with a foreground color."""
        self.cursor_background_priority = cursor_background_priority
        """Should we prioritize the cursor component class CSS background or the renderable background
        in the event where a cell contains a renderable with a background color."""
        self.cursor_type = cursor_type
        """The type of the cursor of the `ArrowTable`."""
        self.cell_padding = cell_padding
        """Horizontal padding between cells, applied on each side of each cell."""

    @property
    def hover_row(self) -> int:
        """The index of the row that the mouse cursor is currently hovering above."""
        return self.hover_coordinate.row

    @property
    def hover_column(self) -> int:
        """The index of the column that the mouse cursor is currently hovering above."""
        return self.hover_coordinate.column

    @property
    def cursor_row(self) -> int:
        """The index of the row that the ArrowTable cursor is currently on."""
        return self.cursor_coordinate.row

    @property
    def cursor_column(self) -> int:
        """The index of the column that the ArrowTable cursor is currently on."""
        return self.cursor_coordinate.column

    @property
    def row_count(self) -> int:
        """The total number of rows currently present in the ArrowTable."""
        return cast(int, self._table.num_rows)

    @property
    def column_count(self) -> int:
        """The total number of columns currently present in the ArrowTable."""
        return cast(int, self._table.num_columns)

    def get_cell_at(self, coordinate: Coordinate) -> pa.Scalar:
        """Get the value from the cell occupying the given coordinate.

        Args:
            coordinate: The coordinate to retrieve the value from.

        Returns:
            The value of the cell at the coordinate.

        Raises:
            IndexError: If there is no cell with the given coordinate.
        """
        row, column = coordinate.row, coordinate.column

        if row < 0 or row >= self.row_count:
            raise IndexError(coordinate)
        if column < 0 or column >= self.column_count:
            raise IndexError(coordinate)

        return self._table.column(column)[row]

    def _measure_column_content_width(self, column: pa.ChunkedArray) -> int:
        widths: list[int] = [
            measure(self.app.console, format_cell(scalar), 1)  # pyright: ignore
            for scalar in column
        ]

        if not widths:
            # Fallback to header/min width when the sampled column has no values.
            return 0

        index = ceil(len(widths) * _COLUMN_WIDTH_PERCENTILE) - 1
        return sorted(widths)[index]

    def _build_columns(self, table: pa.Table) -> tuple[ArrowColumn, ...]:
        row_indices = _sample_row_indices(self.row_count)
        sample_table = table.take(pa.array(row_indices, type=pa.int64()))

        return tuple(
            ArrowColumn(name, self._measure_column_content_width(column))
            for name, column in zip(
                sample_table.column_names, sample_table.columns, strict=True
            )
        )

    def _measure_index_column_width(self) -> int:
        if not self.show_row_index:
            return 0

        max_row_index = max(self.row_count - 1, 0)
        return len(str(max_row_index)) + 2 * self.cell_padding

    def _update_dimensions(self) -> None:
        """Called to recalculate the virtual (scrollable) size."""
        self._columns = self._build_columns(self._table)
        self._column_widths = tuple(
            column.get_render_width(self.cell_padding) for column in self._columns
        )
        self._index_column_width = self._measure_index_column_width()
        self._column_offsets = tuple(
            self._index_column_width + offset
            for offset in accumulate(self._column_widths, initial=0)
        )[:-1]

        total_width = sum(self._column_widths) + self._index_column_width
        header_height = self.header_height if self.show_header else 0
        self.virtual_size = Size(total_width, self.row_count + header_height)

    async def _on_idle(self, event: events.Idle) -> None:
        """Recompute table layout when Textual is idle."""
        _ = event

        if self._require_update_dimensions:
            self._require_update_dimensions = False
            self._update_dimensions()

    def _get_header_style(self, base_style: Style) -> Style:
        """Get the Style that should be applied to the header row."""
        return base_style + self.get_component_styles("arrowtable--header").rich_style

    def _get_row_style(self, row_index: int, base_style: Style) -> Style:
        """Get the Style that should be applied to the row at the given index.

        Args:
            row_index: The index of the row to style.
            base_style: The base style to use by default.

        Returns:
            The appropriate style.
        """
        if not self.zebra_stripes:
            return base_style

        component_class = (
            "arrowtable--even-row" if row_index % 2 == 0 else "arrowtable--odd-row"
        )
        return base_style + self.get_component_styles(component_class).rich_style

    def _render_cell_renderable(
        self, renderable: RenderableType, base_style: Style, width: int
    ) -> list[Segment]:
        """Render a Rich renderable into a single fixed-width table cell."""
        options = self.app.console_options.update_dimensions(width, 1)  # pyright: ignore
        options = options.update(no_wrap=True, overflow="ellipsis")

        lines = self.app.console.render_lines(  # pyright: ignore
            Styled(
                Padding(renderable, (0, self.cell_padding)),
                pre_style=base_style,
                post_style="",
            ),
            options,
        )

        return lines[0] if lines else [Segment(" " * width, base_style)]

    def _render_cell(
        self, scalar: pa.Scalar, base_style: Style, width: int
    ) -> list[Segment]:
        """Render one Arrow scalar into fixed-width cell segments."""
        return self._render_cell_renderable(format_cell(scalar), base_style, width)

    def _render_text_cell(
        self, text: str, base_style: Style, width: int
    ) -> list[Segment]:
        """Render plain text into fixed-width cell segments."""
        return self._render_cell_renderable(Text(text), base_style, width)

    def _render_header_line(self, base_style: Style) -> Strip:
        """Render the table header line."""
        segments: list[Segment] = []
        header_style = self._get_header_style(base_style)

        if self.show_row_index:
            segments.extend(
                self._render_text_cell("", header_style, self._index_column_width)
            )

        for column, width in zip(self._columns, self._column_widths, strict=True):
            segments.extend(self._render_text_cell(column.name, header_style, width))

        return Strip(segments)

    def _render_row_line(self, row_index: int, base_style: Style) -> Strip:
        """Render one data row."""
        segments: list[Segment] = []
        row_style = self._get_row_style(row_index, base_style)

        if self.show_row_index:
            segments.extend(
                self._render_text_cell(
                    str(row_index), row_style, self._index_column_width
                )
            )

        for column_index, width in enumerate(self._column_widths):
            scalar = self.get_cell_at(Coordinate(row_index, column_index))
            segments.extend(self._render_cell(scalar, row_style, width))

        return Strip(segments)

    def _render_line(self, y: int, x1: int, x2: int, base_style: Style) -> Strip:
        """Render a (possibly cropped) line into a Strip.

        Strip is an immutable list of segments representing a horizontal line.

        Args:
            y: Y coordinate of line relative to ArrowTable top.
            x1: X start crop.
            x2: X end crop (exclusive).
            base_style: Style to apply to line.

        Returns:
            The Strip which represents this cropped line.
        """
        width = self.size.width

        if not self._columns:
            return Strip.blank(width, base_style)

        if self.show_header and y < self.header_height:
            strip = self._render_header_line(base_style)
        else:
            row_index = y - (self.header_height if self.show_header else 0)
            if row_index < 0 or row_index >= self.row_count:
                return Strip.blank(width, base_style)

            strip = self._render_row_line(row_index, base_style)

        return strip.crop(x1, x2).adjust_cell_length(width, base_style).simplify()

    def render_lines(self, crop: Region) -> list[Strip]:
        """Render the widget into lines.

        Args:
            crop: Region within visible area to render.

        Returns:
            A list of list of segments.
        """
        self._pseudo_class_state = self.get_pseudo_class_state()
        return super().render_lines(crop)

    def render_line(self, y: int) -> Strip:
        """Render a line of content.

        Args:
            y: Y Coordinate of line.

        Returns:
            A rendered line.
        """
        width, _ = self.size
        # `y` is the line within the widget's currently visible content area.
        # `scroll_y` is the vertical offset into the scrollable table body.
        scroll_x, scroll_y = self.scroll_offset

        header_height = self.header_height if self.show_header else 0
        # `table_y` maps the visible line to the table's virtual space, keeping
        # the header pinned while data rows scroll.
        table_y = y if y < header_height else y + scroll_y

        return self._render_line(table_y, scroll_x, scroll_x + width, self.rich_style)
