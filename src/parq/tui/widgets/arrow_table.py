"""The ArrowTable widget."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from itertools import chain
from math import ceil
from typing import ClassVar, Literal, NamedTuple, cast

import pyarrow as pa
from rich.cells import cell_len
from rich.console import Console, RenderableType
from rich.filesize import decimal
from rich.padding import Padding
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding, BindingType
from textual.cache import LRUCache
from textual.coordinate import Coordinate
from textual.geometry import Region, Size
from textual.message import Message
from textual.reactive import Reactive
from textual.render import measure
from textual.renderables.styled import Styled
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.types import NoActiveAppError
from textual.widget import PseudoClasses

logger = logging.getLogger(__name__)

type CursorType = Literal["cell", "row", "column", "none"]


class RowCacheKey(NamedTuple):
    """Cache key for rendered fixed and scrollable segments in a row."""

    row_index: int
    base_style: Style
    cursor_location: Coordinate
    hover_location: Coordinate
    cursor_type: CursorType
    show_cursor: bool
    show_hover_cursor: bool
    update_count: int
    pseudo_class_state: PseudoClasses


class CellCacheKey(NamedTuple):
    """Cache key for rendered segment lines in a cell."""

    row_index: int
    column_index: int
    base_style: Style
    cursor: bool
    """Whether this cell is affected by cursor highlighting."""
    hover: bool
    """Whether this cell is affected by hover cursor highlighting."""
    show_hover_cursor: bool
    update_count: int
    pseudo_class_state: PseudoClasses


class LineCacheKey(NamedTuple):
    """Cache key for a rendered and cropped viewport line."""

    y: int
    """Y coordinate of line relative to virtual table top."""
    x1: int
    x2: int
    width: int
    cursor_coordinate: Coordinate
    hover_coordinate: Coordinate
    base_style: Style
    cursor_type: CursorType
    show_hover_cursor: bool
    update_count: int
    pseudo_class_state: PseudoClasses


def format_cell(scalar: pa.Scalar, binary_inline_limit: int = 16) -> RenderableType:
    """Convert a cell into a Rich renderable for display.

    Args:
        scalar: Arrow scalar for a cell.
        binary_inline_limit: Maximum number of binary bytes to render inline as hex.

    Returns:
        A single-line renderable representing the data.
    """
    if not scalar.is_valid:
        return Text("null", style="dim italic magenta")

    data_type, value = scalar.type, scalar.as_py()

    if (
        pa.types.is_integer(data_type)
        or pa.types.is_floating(data_type)
        or pa.types.is_decimal(data_type)
    ):
        return Text(str(value), style="cyan")

    if pa.types.is_boolean(data_type):
        if value:
            return Text("true", style="green")
        return Text("false", style="red")

    if pa.types.is_temporal(data_type):
        return Text(str(value), style="yellow")

    if (
        pa.types.is_binary(data_type)
        or pa.types.is_large_binary(data_type)
        or pa.types.is_fixed_size_binary(data_type)
    ):
        if len(value) <= binary_inline_limit:
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


def _line_crop(
    segments: list[Segment], start: int, end: int, total: int
) -> list[Segment]:
    """Crops a list of segments between two cell offsets.

    Args:
        segments: A list of Segments for a line.
        start: Start offset (cells)
        end: End offset (cells, exclusive)
        total: Total cell length of segments.

    Returns:
        A new shorter list of segments
    """
    # This is essentially a specialized version of Segment.divide
    # The following line has equivalent functionality (but a little slower)
    # return list(Segment.divide(segments, [start, end]))[1]

    _cell_len = cell_len
    pos = 0
    output_segments: list[Segment] = []
    add_segment = output_segments.append
    iter_segments = iter(segments)
    segment: Segment | None = None
    for segment in iter_segments:
        end_pos = pos + _cell_len(segment.text)
        if end_pos > start:
            segment = segment.split_cells(start - pos)[1]
            break
        pos = end_pos
    else:
        return []

    if end >= total:
        # The end crop is the end of the segments,
        # so we can collect all remaining segments
        if segment:
            add_segment(segment)
        output_segments.extend(iter_segments)
        return output_segments

    pos = start
    while segment is not None:
        end_pos = pos + _cell_len(segment.text)
        if end_pos < end:
            add_segment(segment)
        else:
            add_segment(segment.split_cells(end - pos)[0])
            break
        pos = end_pos
        segment = next(iter_segments, None)

    return output_segments


class RowRenderables(NamedTuple):
    """Container for a row, which contains an optional index and some data cells."""

    idx: RenderableType | None
    cells: list[RenderableType]


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
        Binding("ctrl+home", "scroll_top", "Top", show=False),
        Binding("ctrl+end", "scroll_bottom", "Bottom", show=False),
        Binding("home", "scroll_home", "Home", show=False),
        Binding("end", "scroll_end", "End", show=False),
    ]
    """ArrowTable bindings:
    | Key       | Description                                  |
    | :---      | :---                                         |
    | enter     | Select cells under the cursor.               |
    | up        | Move the cursor up.                          |
    | down      | Move the cursor down.                        |
    | right     | Move the cursor right.                       |
    | left      | Move the cursor left.                        |
    | pageup    | Move one page up.                            |
    | pagedown  | Move one page down.                          |
    | ctrl+home | Move to the top.                             |
    | ctrl+end  | Move to the bottom.                          |
    | home      | Move to the home position (leftmost column). |
    | end       | Move to the end position (rightmost column). |
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
        self._columns: tuple[ArrowColumn, ...] | None = None
        """Column metadata in source column order. Lazily computed in `self.columns`."""

        self._row_render_cache: LRUCache[
            RowCacheKey, tuple[list[list[Segment]], list[list[Segment]]]
        ] = LRUCache(1000)
        """For each row, we maintain a cache of the fixed and scrollable lines within that row 
        to minimize how often we need to re-render it. """
        self._cell_render_cache: LRUCache[CellCacheKey, list[list[Segment]]] = LRUCache(
            10000
        )
        """Cache for individual cells."""
        self._row_renderable_cache: LRUCache[int, RowRenderables] = LRUCache(1000)
        """Caches row renderables - key is row_index."""
        self._line_cache: LRUCache[LineCacheKey, Strip] = LRUCache(1000)
        """Cache for lines within rows."""

        self._pseudo_class_state = PseudoClasses(False, False, False)
        """The pseudo-class state is used as part of cache keys to ensure that, for example,
        when we lose focus on the ArrowTable, rules which apply to :focus are invalidated
        and we prevent lingering styles."""

        self._require_update_dimensions = True
        """Set to re-calculate dimensions on idle."""

        self._show_hover_cursor = False
        """Used to hide the mouse hover cursor when the user uses the keyboard."""
        self._update_count = 0
        """Number of updates so far. Used for cache invalidation."""
        self._header_row_index = -1
        """The header is a special row - not part of the data."""
        self._index_column_index = -1
        """The column containing row index is not part of the data."""
        self._index_column: ArrowColumn | None = None
        """The largest content width out of all row indices in the table.
        Lazily computed in `self.index_column`."""

        self.show_header = show_header
        """Show/hide the header row (the row of column labels)."""
        self.show_row_index = show_row_index
        """Show/hide the row index column containing zero-based row numbers."""
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

    def _measure_content_width(
        self,
        column: pa.ChunkedArray,
        sample_indices: pa.Array,
        percentile: float = 0.95,
    ) -> int:
        """Estimate the display width of a column's formatted cell content.

        The estimate is based on the terminal-cell width of sampled, formatted
        values. Some Arrow types use constant-width shortcuts when their display
        width is known without scanning sampled values.

        Args:
            column: Arrow column to measure.
            sample_indices: Row indices used to sample values from the column.
            percentile: Percentile of sampled widths to return, expressed as a
                value in the range ``0 < percentile <= 1``.

        Returns:
            Estimated content width in terminal cells, excluding horizontal cell
            padding.
        """
        data_type = column.type
        try:
            console = self.app.console  # pyright: ignore
        except NoActiveAppError:
            console = Console()  # Use a fallback console

        # Some types can be measured more efficiently
        if pa.types.is_boolean(data_type):
            return 5  # "false"
        if pa.types.is_null(data_type):
            return 4  # "null"

        # For everything else, we need to compute it
        widths: list[int] = [
            measure(console, format_cell(scalar), 0)
            for scalar in column.take(sample_indices)
        ]

        if len(widths) == 0:
            return 0

        index = ceil(len(widths) * percentile) - 1
        return sorted(widths)[index]

    @property
    def columns(self) -> tuple[ArrowColumn, ...]:
        """Metadata about the columns of the arrow."""
        if self._columns is not None:
            return self._columns

        row_indices = _sample_row_indices(self.row_count)
        sample_indices = pa.array(row_indices, type=pa.int64())

        self._columns = tuple(
            ArrowColumn(name, self._measure_content_width(column, sample_indices))
            for name, column in zip(
                self._table.column_names, self._table.columns, strict=True
            )
        )
        return self._columns

    @property
    def _column_widths(self) -> tuple[int, ...]:
        """Rendered column widths, including horizontal padding."""
        return tuple(
            column.get_render_width(self.cell_padding) for column in self.columns
        )

    @property
    def index_column(self) -> ArrowColumn:
        """Virtual column metadata for the row-index column."""
        if self._index_column is not None:
            return self._index_column

        max_row_index = max(self.row_count - 1, 0)
        content_width = len(str(max_row_index))
        self._index_column = ArrowColumn("#", content_width)

        return self._index_column

    @property
    def _index_column_width(self) -> int:
        """The render width of the column containing row indices."""
        return (
            self.index_column.get_render_width(self.cell_padding)
            if self.show_row_index
            else 0
        )

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

        if not self.is_valid_row_index(row):
            raise IndexError(coordinate)
        if not self.is_valid_column_index(column):
            raise IndexError(coordinate)

        return self._table.column(column)[row]

    def _on_resize(self, _: events.Resize) -> None:
        self._update_count += 1
        logger.debug(
            "App or widget has been resized. ArrowTable._update_count: %d",
            self._update_count,
        )

    def _update_dimensions(self) -> None:
        """Called to recalculate the virtual (scrollable) size."""
        total_width = sum(self._column_widths) + self._index_column_width
        header_lines = 1 if self.show_header else 0
        self.virtual_size = Size(total_width, self.row_count + header_lines)

    async def _on_idle(self, event: events.Idle) -> None:
        """Runs when the message pump is empty.

        We use this for some expensive calculations like re-computing dimensions of the
        whole ArrowTable and re-computing column widths after some cells
        have been updated. This is more efficient in the case of high
        frequency updates, ensuring we only do expensive computations once.
        """
        _ = event

        if self._require_update_dimensions:
            self._require_update_dimensions = False
            self._update_dimensions()

    def is_valid_row_index(self, row_index: int) -> bool:
        """Return a boolean indicating whether the row_index is within table bounds.

        Args:
            row_index: The row index to check.

        Returns:
            True if the row index is within the bounds of the table.
        """
        return 0 <= row_index < self.row_count

    def is_valid_column_index(self, column_index: int) -> bool:
        """Return a boolean indicating whether the column_index is within table bounds.

        Args:
            column_index: The column index to check.

        Returns:
            True if the column index is within the bounds of the table.
        """
        return 0 <= column_index < self.column_count

    def _get_row_renderables(self, row_index: int) -> RowRenderables:
        """Get renderables for the row currently at the given row index.

        The renderables returned here have already been passed through the `format_cell`.

        Args:
            row_index: Index of the row.

        Returns:
            A RowRenderables containing the optional label and the rendered cells.
        """
        cache_key = row_index
        if cache_key in self._row_renderable_cache:
            return self._row_renderable_cache[cache_key]

        if row_index == self._header_row_index:
            renderables = RowRenderables(
                None, [Text(column.name) for column in self.columns]
            )
            self._row_renderable_cache[cache_key] = renderables
            return renderables

        if not self.is_valid_row_index(row_index):
            return RowRenderables(None, [])

        renderables = RowRenderables(
            Text(str(row_index), style="dim"),
            [
                format_cell(self.get_cell_at(Coordinate(row_index, column_index)))
                for column_index in range(self.column_count)
            ],
        )
        self._row_renderable_cache[cache_key] = renderables
        return renderables

    def _render_cell(
        self,
        row_index: int,
        column_index: int,
        base_style: Style,
        width: int,
        cursor: bool = False,
        hover: bool = False,
    ) -> list[list[Segment]]:
        """Render the given cell.

        Args:
            row_index: Index of the row.
            column_index: Index of the column.
            base_style: Style to apply.
            width: Width of the cell.
            cursor: Whether this cell is affected by cursor highlighting.
            hover: Whether this cell is affected by hover cursor highlighting.

        Returns:
            A list of segments per line.
        """
        is_header_cell = row_index == self._header_row_index
        is_row_index_cell = column_index == self._index_column_index

        cache_key = CellCacheKey(
            row_index,
            column_index,
            base_style,
            cursor,
            hover,
            self._show_hover_cursor,
            self._update_count,
            self._pseudo_class_state,
        )

        if cache_key not in self._cell_render_cache:
            console = self.app.console  # pyright: ignore
            base_style += Style.from_meta({"row": row_index, "column": column_index})

            index_renderable, row_cells = self._get_row_renderables(row_index)

            if is_row_index_cell:
                cell = index_renderable if index_renderable is not None else ""
            else:
                cell = row_cells[column_index]

            component_style, post_style = self._get_styles_to_render_cell(
                is_header_cell,
                is_row_index_cell,
                hover,
                cursor,
                self.show_cursor,
                self._show_hover_cursor,
                self.cursor_foreground_priority == "css",
                self.cursor_background_priority == "css",
            )

            options = console.options.update_dimensions(width, 1).update(
                no_wrap=True, overflow="ellipsis"
            )

            lines = console.render_lines(
                Styled(
                    Padding(cell, (0, self.cell_padding)),
                    pre_style=base_style + component_style,
                    post_style=post_style,
                ),
                options,
            )

            self._cell_render_cache[cache_key] = lines

        return self._cell_render_cache[cache_key]

    def _get_styles_to_render_cell(
        self,
        is_header_cell: bool,
        is_row_index_cell: bool,
        hover: bool,
        cursor: bool,
        show_cursor: bool,
        show_hover_cursor: bool,
        has_css_foreground_priority: bool,
        has_css_background_priority: bool,
    ) -> tuple[Style, Style]:
        """Auxiliary method to compute styles used to render a given cell.

        Args:
            is_header_cell: Is this a cell from a header?
            is_row_index_cell: Is this the label of any given row?
            hover: Does this cell have the hover pseudo class?
            cursor: Is this cell covered by the cursor?
            show_cursor: Do we want to show the cursor in the data table?
            show_hover_cursor: Do we want to show the mouse hover when using the keyboard
                to move the cursor?
            has_css_foreground_priority: `self.cursor_foreground_priority == "css"`?
            has_css_background_priority: `self.cursor_background_priority == "css"`?

        Returns:
            A pair of styles to apply before and after rendering the cell content.
        """
        component_style = Style()

        if hover and show_cursor and show_hover_cursor:
            component_style += self.get_component_rich_style("arrowtable--hover")
            if is_header_cell or is_row_index_cell:
                # Apply subtle variation in style for the header/label (blue
                # background by default) rows and columns affected by the cursor, to
                # ensure we can still differentiate between the indices and the data.
                component_style += self.get_component_rich_style(
                    "arrowtable--header-hover"
                )

        if cursor and show_cursor:
            cursor_style = self.get_component_rich_style("arrowtable--cursor")
            component_style += cursor_style
            if is_header_cell or is_row_index_cell:
                component_style += self.get_component_rich_style(
                    "arrowtable--header-cursor"
                )

        post_foreground = (
            Style.from_color(color=component_style.color)
            if has_css_foreground_priority
            else Style.null()
        )
        post_background = (
            Style.from_color(bgcolor=component_style.bgcolor)
            if has_css_background_priority
            else Style.null()
        )

        return component_style, post_foreground + post_background

    def _render_line_in_row(
        self,
        row_index: int,
        base_style: Style,
        cursor_location: Coordinate,
        hover_location: Coordinate,
    ) -> tuple[list[list[Segment]], list[list[Segment]]]:
        """Render a single line from a row in the ArrowTable.

        Args:
            row_index: The 0-based index for this row.
            base_style: Base style of row.
            cursor_location: The location of the cursor in the ArrowTable.
            hover_location: The location of the hover cursor in the ArrowTable.

        Returns:
            Lines for fixed cells, and Lines for scrollable cells.
        """
        cursor_type = self.cursor_type
        show_cursor = self.show_cursor

        cache_key = RowCacheKey(
            row_index,
            base_style,
            cursor_location,
            hover_location,
            cursor_type,
            show_cursor,
            self._show_hover_cursor,
            self._update_count,
            self._pseudo_class_state,
        )

        if cache_key in self._row_render_cache:
            return self._row_render_cache[cache_key]

        header_style = self.get_component_styles("arrowtable--header").rich_style

        # If the row has a index, add it to fixed_row here with correct style.
        fixed_row: list[list[Segment]] = []

        if self.show_row_index:
            # The width of the row index is updated again on idle
            cell_location = Coordinate(row_index, self._index_column_index)
            index_cell_lines = self._render_cell(
                row_index,
                self._index_column_index,
                header_style,
                width=self._index_column_width,
                cursor=self._should_highlight(
                    cursor_location, cell_location, cursor_type
                ),
                hover=self._should_highlight(
                    hover_location, cell_location, cursor_type
                ),
            )[0]  # Only single line for a cell.
            fixed_row.append(index_cell_lines)

        row_style = self._get_row_style(row_index, base_style)

        scrollable_row: list[list[Segment]] = []

        for column_index, column in enumerate(self.columns):
            cell_location = Coordinate(row_index, column_index)
            cell_lines = self._render_cell(
                row_index,
                column_index,
                row_style,
                width=column.get_render_width(self.cell_padding),
                cursor=self._should_highlight(
                    cursor_location, cell_location, cursor_type
                ),
                hover=self._should_highlight(
                    hover_location, cell_location, cursor_type
                ),
            )[0]
            scrollable_row.append(cell_lines)

        row_pair = (fixed_row, scrollable_row)
        self._row_render_cache[cache_key] = row_pair
        return row_pair

    def _render_line(self, y: int, x1: int, x2: int, base_style: Style) -> Strip:
        """Render a (possibly cropped) line into a Strip.

        Strip is like an immutable list of segments representing a horizontal line.

        Args:
            y: Y coordinate of line relative to virtual table top.
            x1: X start crop.
            x2: X end crop (exclusive).
            base_style: Style to apply to line.

        Returns:
            The Strip which represents this cropped line.
        """
        width = self.size.width
        header_lines = 1 if self.show_header else 0
        row_index = (
            self._header_row_index if self.show_header and y == 0 else y - header_lines
        )
        if (
            not self.is_valid_row_index(row_index)
            and row_index != self._header_row_index
        ):
            return Strip.blank(width, base_style)

        cache_key = LineCacheKey(
            y,
            x1,
            x2,
            width,
            self.cursor_coordinate,
            self.hover_coordinate,
            base_style,
            self.cursor_type,
            self._show_hover_cursor,
            self._update_count,
            self._pseudo_class_state,
        )
        if cache_key in self._line_cache:
            return self._line_cache[cache_key]

        fixed, scrollable = self._render_line_in_row(
            row_index,
            base_style,
            cursor_location=self.cursor_coordinate,
            hover_location=self.hover_coordinate,
        )

        fixed_line: list[Segment] = list(chain.from_iterable(fixed)) if fixed else []
        scrollable_line: list[Segment] = list(chain.from_iterable(scrollable))

        fixed_width = self._index_column_width
        scrollable_width = sum(self._column_widths)
        visible_scrollable_width = max(0, width - fixed_width)

        segments = fixed_line + _line_crop(
            scrollable_line, x1, x1 + visible_scrollable_width, scrollable_width
        )
        strip = Strip(segments).adjust_cell_length(width, base_style).simplify()

        self._line_cache[cache_key] = strip
        return strip

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
            y: Y Coordinate of line relative to widget's visible area top.

        Returns:
            A rendered line.
        """
        width, _ = self.size
        # Horizontal and vertical offset into the scrollable table body.
        scroll_x, scroll_y = self.scroll_offset

        # `table_y` maps the visible line to the table's virtual table space, keeping
        # the header pinned while data rows scroll.
        table_y = y if self.show_header and y == 0 else y + scroll_y

        return self._render_line(table_y, scroll_x, scroll_x + width, self.rich_style)

    def _should_highlight(
        self, cursor: Coordinate, target_cell: Coordinate, type_of_cursor: CursorType
    ) -> bool:
        """Determine if the given cell should be highlighted because of the cursor.

        This auxiliary method takes the cursor position and type into account when
        determining whether the cell should be highlighted.

        Args:
            cursor: The current position of the cursor.
            target_cell: The cell we're checking for the need to highlight.
            type_of_cursor: The type of cursor that is currently active.

        Returns:
            Whether or not the given cell should be highlighted.
        """
        if type_of_cursor == "cell":
            return cursor == target_cell
        if type_of_cursor == "row":
            cursor_row, _ = cursor
            cell_row, _ = target_cell
            return cursor_row == cell_row
        if type_of_cursor == "column":
            _, cursor_column = cursor
            _, cell_column = target_cell
            return cursor_column == cell_column
        return False

    def _get_row_style(self, row_index: int, base_style: Style) -> Style:
        """Gets the Style that should be applied to the row at the given index.

        Args:
            row_index: The index of the row to style.
            base_style: The base style to use by default.

        Returns:
            The appropriate style.
        """
        if row_index == self._header_row_index:
            return self.get_component_styles("arrowtable--header").rich_style

        if self.zebra_stripes:
            component_row_style = (
                "arrowtable--even-row" if row_index % 2 == 0 else "arrowtable--odd-row"
            )
            return self.get_component_styles(component_row_style).rich_style

        return base_style
