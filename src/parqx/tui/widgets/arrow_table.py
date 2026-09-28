"""Interactive Arrow-backed table widget."""

# Derived from Textual's DataTable implementation:
# https://github.com/Textualize/textual/blob/v8.2.7/src/textual/widgets/_data_table.py
#
# Textual is licensed under the MIT License.
# Copyright (c) 2021 Will McGugan.
#
# Modifications copyright (c) 2026 Senyu Xie.

from __future__ import annotations

import contextlib
import logging
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from itertools import accumulate
from math import ceil
from typing import ClassVar, Literal, NamedTuple, Self

import pyarrow as pa
import rich.repr
from rich.cells import cell_len
from rich.console import Console
from rich.padding import Padding
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding, BindingType
from textual.cache import LRUCache
from textual.coordinate import Coordinate
from textual.geometry import Region, Size, Spacing, clamp
from textual.message import Message
from textual.reactive import Reactive
from textual.renderables.styled import Styled
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.types import NoActiveAppError
from textual.widget import PseudoClasses

from parqx.data.view import DataPage, TableData
from parqx.tui.cell_formatter import CellFormatter

logger = logging.getLogger(__name__)

PREFETCH_ROWS = 256
WIDTH_MEASUREMENT_ROWS = 256

type CursorType = Literal["cell", "row", "column", "none"]


class RowCacheKey(NamedTuple):
    """Cache key for rendered fixed and scrollable segments in a row."""

    row_index: int
    base_style: Style
    cursor_coordinate: Coordinate
    hover_coordinate: Coordinate
    cursor_type: CursorType
    show_cursor: bool
    show_hover_cursor: bool
    update_count: int
    pseudo_class_state: PseudoClasses
    start_column: int
    stop_column: int


class CellCacheKey(NamedTuple):
    """Cache key for rendered segments in a cell."""

    row_index: int
    column_index: int
    base_style: Style
    cursor: bool
    hover: bool
    update_count: int
    pseudo_class_state: PseudoClasses


class LineCacheKey(NamedTuple):
    """Cache key for a rendered and cropped viewport line."""

    y: int
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


class RenderedRow(NamedTuple):
    """Single-line segments before viewport cropping; the row index stays fixed."""

    fixed: list[Segment]
    scrollable: list[Segment]


class CellNotExistError(Exception):
    """The coordinate is outside the table bounds."""


class CellNotLoadedError(CellNotExistError):
    """The coordinate exists but its data window has not arrived yet."""


@dataclass
class Column:
    """Metadata for a column in the ArrowTable."""

    name: str
    """Column name from the Arrow schema."""
    width: int = 0
    """Fixed content width if auto_width is false."""
    content_width: int = 0
    """Estimated terminal-cell width over formatted cell values."""
    auto_width: bool = True
    """Whether render width is based on measured content_width."""


def _sample_row_indices(
    total_rows: int, target_count: int = WIDTH_MEASUREMENT_ROWS
) -> tuple[int, ...]:
    """Return deterministic row indices sampled from evenly spaced rows."""
    if total_rows <= 0 or target_count <= 0:
        return ()
    if total_rows <= target_count:
        return tuple(range(total_rows))
    if target_count == 1:
        return (0,)

    last = total_rows - 1
    return tuple(round(i * last / (target_count - 1)) for i in range(target_count))


class ArrowTable(ScrollView, can_focus=True):
    """Arrow-backed data table widget."""

    class WindowRequested(Message):
        """Request background I/O after render-time cache misses are coalesced."""

        def __init__(self, data: TableData, start: int, stop: int) -> None:
            """Capture the data identity so a replaced view can reject this read."""
            super().__init__()
            self.data = data
            self.start_row = start
            self.stop_row = stop

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

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "arrowtable--cursor",
        "arrowtable--hover",
        "arrowtable--header",
        "arrowtable--header-cursor",
        "arrowtable--header-hover",
        "arrowtable--odd-row",
        "arrowtable--even-row",
    }

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
        """Posted for a loaded cell when its cursor moves or is re-enabled."""

        def __init__(
            self, arrow_table: ArrowTable, value: pa.Scalar, coordinate: Coordinate
        ) -> None:
            """Initialize a cell highlighted message."""
            self.arrow_table = arrow_table
            self.value = value
            self.coordinate: Coordinate = coordinate
            super().__init__()

        def __rich_repr__(self) -> rich.repr.Result:
            """Yield fields for Rich's object representation."""
            yield "value", self.value
            yield "coordinate", self.coordinate

        @property
        def control(self) -> ArrowTable:
            """Alias for the data table."""
            return self.arrow_table

    class CellSelected(Message):
        """Posted when a cell is selected in cell cursor mode."""

        def __init__(
            self, arrow_table: ArrowTable, value: pa.Scalar, coordinate: Coordinate
        ) -> None:
            """Initialize a cell selected message."""
            self.arrow_table = arrow_table
            self.value: pa.Scalar = value
            self.coordinate: Coordinate = coordinate
            super().__init__()

        def __rich_repr__(self) -> rich.repr.Result:
            """Yield fields for Rich's object representation."""
            yield "value", self.value
            yield "coordinate", self.coordinate

        @property
        def control(self) -> ArrowTable:
            """Alias for the data table."""
            return self.arrow_table

    class RowHighlighted(Message):
        """Posted when a row is highlighted in row cursor mode."""

        def __init__(self, arrow_table: ArrowTable, cursor_row: int) -> None:
            """Initialize a row highlighted message."""
            self.arrow_table = arrow_table
            self.cursor_row: int = cursor_row
            super().__init__()

        def __rich_repr__(self) -> rich.repr.Result:
            """Yield fields for Rich's object representation."""
            yield "cursor_row", self.cursor_row

        @property
        def control(self) -> ArrowTable:
            """Alias for the data table."""
            return self.arrow_table

    class RowSelected(Message):
        """Posted when a row is selected in row cursor mode."""

        def __init__(self, arrow_table: ArrowTable, cursor_row: int) -> None:
            """Initialize a row selected message."""
            self.arrow_table = arrow_table
            self.cursor_row: int = cursor_row
            super().__init__()

        def __rich_repr__(self) -> rich.repr.Result:
            """Yield fields for Rich's object representation."""
            yield "cursor_row", self.cursor_row

        @property
        def control(self) -> ArrowTable:
            """Alias for the data table."""
            return self.arrow_table

    class ColumnHighlighted(Message):
        """Posted when a column is highlighted in column cursor mode."""

        def __init__(self, arrow_table: ArrowTable, cursor_column: int) -> None:
            """Initialize a column highlighted message."""
            self.arrow_table = arrow_table
            self.cursor_column: int = cursor_column
            super().__init__()

        def __rich_repr__(self) -> rich.repr.Result:
            """Yield fields for Rich's object representation."""
            yield "cursor_column", self.cursor_column

        @property
        def control(self) -> ArrowTable:
            """Alias for the data table."""
            return self.arrow_table

    class ColumnSelected(Message):
        """Posted when a column is selected in column cursor mode."""

        def __init__(self, arrow_table: ArrowTable, cursor_column: int) -> None:
            """Initialize a column selected message."""
            self.arrow_table = arrow_table
            self.cursor_column: int = cursor_column
            super().__init__()

        def __rich_repr__(self) -> rich.repr.Result:
            """Yield fields for Rich's object representation."""
            yield "cursor_column", self.cursor_column

        @property
        def control(self) -> ArrowTable:
            """Alias for the data table."""
            return self.arrow_table

    class HeaderSelected(Message):
        """Posted when a column header/label is clicked."""

        def __init__(self, arrow_table: ArrowTable, column_index: int, label: Text):
            """Initialize a header selected message."""
            self.arrow_table = arrow_table
            self.column_index = column_index
            self.label = label
            super().__init__()

        def __rich_repr__(self) -> rich.repr.Result:
            """Yield fields for Rich's object representation."""
            yield "column_index", self.column_index
            yield "label", self.label.plain

        @property
        def control(self) -> ArrowTable:
            """Alias for the data table."""
            return self.arrow_table

    class RowIndexSelected(Message):
        """Posted when a row index cell is clicked."""

        def __init__(self, arrow_table: ArrowTable, row_index: int):
            """Initialize a row-index selected message."""
            self.arrow_table = arrow_table
            self.row_index = row_index
            super().__init__()

        def __rich_repr__(self) -> rich.repr.Result:
            """Yield fields for Rich's object representation."""
            yield "row_index", self.row_index

        @property
        def control(self) -> ArrowTable:
            """Alias for the data table."""
            return self.arrow_table

    def __init__(
        self,
        table: pa.Table | TableData,
        show_header: bool = True,
        show_row_index: bool = True,
        zebra_stripes: bool = False,
        show_cursor: bool = True,
        cursor_foreground_priority: Literal["renderable", "css"] = "css",
        cursor_background_priority: Literal["renderable", "css"] = "renderable",
        cursor_type: CursorType = "cell",
        cell_padding: int = 1,
        max_column_content_width: int = 48,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        """Create a cache-only table with single-line cells and bounded column widths.

        `table` may be a bounded in-memory Arrow table or a lazy TableData view.
        Cursor color priorities choose whether component CSS or formatted content
        wins. `max_column_content_width` caps content, excluding cell padding.
        Name, id, classes, and disabled are passed to Textual.
        """
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)

        # Cache-only access; the app owns background I/O.
        self.data = (
            table if isinstance(table, TableData) else TableData.from_table(table)
        )
        self._missing_rows: set[int] = set()
        self._requested_window: tuple[int, int] | None = None
        self._columns: tuple[Column, ...] | None = None
        # Data-column left edges, excluding the index; includes the final right edge.
        self._column_offsets: tuple[int, ...] | None = None
        self._max_column_content_width = max_column_content_width
        self._cell_formatter = CellFormatter(inline_limit=max_column_content_width)

        self._row_render_cache: LRUCache[RowCacheKey, RenderedRow] = LRUCache(1000)
        self._cell_render_cache: LRUCache[CellCacheKey, list[Segment]] = LRUCache(10000)
        # Formatted values survive style/geometry changes, but never data replacement.
        self._cell_renderable_cache: LRUCache[Coordinate, Text] = LRUCache(10000)
        self._line_cache: LRUCache[LineCacheKey, Strip] = LRUCache(1000)

        # Focus/hover state participates in render keys to prevent stale highlighting.
        self._pseudo_class_state = PseudoClasses(False, False, False)

        # Dimension recalculation is deferred until idle.
        self._require_update_dimensions = True

        # Keyboard navigation hides the mouse highlight.
        self._show_hover_cursor = False
        self._update_count = 0
        # Header and index coordinates use -1 outside the data bounds.
        self._header_row_index = -1
        self._index_column_index = -1
        self._index_column: Column | None = None

        self.show_header = show_header
        self.show_row_index = show_row_index
        self.zebra_stripes = zebra_stripes
        self.show_cursor = show_cursor
        self.cursor_foreground_priority = cursor_foreground_priority
        self.cursor_background_priority = cursor_background_priority
        self.cursor_type = cursor_type
        self.cell_padding = cell_padding

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
        return self.data.row_count

    @property
    def _total_row_height(self) -> int:
        """The total height of all rows within the ArrowTable, NOT including the header."""
        return self.row_count

    @property
    def column_count(self) -> int:
        """The total number of columns currently present in the ArrowTable."""
        return len(self.data.schema)

    def _measure_content_width(
        self,
        column: pa.ChunkedArray,
        sample_indices: pa.Array,
        percentile: float = 0.95,
    ) -> int:
        """Estimate the display width of a column's formatted cell content.

        The estimate is based on the terminal-cell width of sampled, formatted
        values. Boolean and null columns use constant-width shortcuts. No data
        values outside the bounded sample are scanned.

        Args:
            column: Arrow column to measure.
            sample_indices: Row indices used to sample values from the column.
            percentile: Percentile of sampled widths to return, expressed as a
                value in the range `0 < percentile <= 1`.

        Returns:
            Estimated content width in terminal cells, excluding horizontal cell
            padding.
        """
        data_type = column.type
        null_width = 4 if column.null_count else 0

        # Some types can be measured more efficiently
        if pa.types.is_boolean(data_type):
            return 5  # "false"
        if pa.types.is_null(data_type):
            return 4  # "null"
        widths: list[int] = [
            cell_len(self._cell_formatter(scalar).plain)
            for scalar in column.take(sample_indices)
        ]

        if not widths:
            return null_width

        index = ceil(len(widths) * percentile) - 1
        percentile_width = sorted(widths)[index]
        return max(null_width, percentile_width)

    @property
    def columns(self) -> tuple[Column, ...]:
        """Metadata about the columns of the arrow."""
        if self._columns is not None:
            return self._columns

        row_indices = _sample_row_indices(self.data.sample.num_rows)
        sample_indices = pa.array(row_indices, type=pa.int64())

        self._columns = tuple(
            Column(
                name, content_width=self._measure_content_width(column, sample_indices)
            )
            for name, column in zip(
                self.data.schema.names, self.data.sample.columns, strict=True
            )
        )
        return self._columns

    def _get_column_render_width(self, column: Column) -> int:
        """Get the render width of a column, including horizontal padding."""
        if not column.auto_width:
            content_render_width = column.width
        else:
            width = max(cell_len(column.name), column.content_width)
            content_render_width = min(width, self._max_column_content_width)

        return content_render_width + 2 * self.cell_padding

    def _get_column_offsets(self) -> tuple[int, ...]:
        if self._column_offsets is None:
            self._column_offsets = tuple(
                accumulate(map(self._get_column_render_width, self.columns), initial=0)
            )
        return self._column_offsets

    def _visible_column_range(self, x1: int, viewport_width: int) -> tuple[int, int]:
        """Return [col_first, col_last_exclusive) of data columns intersecting [x1, x1+viewport_width).

        Coordinates are in the scrollable area's coordinate system (offsets[0]=0),
        NOT including the row-index column.
        """
        if self.column_count == 0 or viewport_width <= 0:
            return 0, 0
        offsets = self._get_column_offsets()
        start_column = max(0, min(self.column_count - 1, bisect_right(offsets, x1) - 1))
        stop_column = min(self.column_count, bisect_left(offsets, x1 + viewport_width))
        if stop_column <= start_column:
            stop_column = min(self.column_count, start_column + 1)
        return start_column, stop_column

    @property
    def index_column(self) -> Column:
        """Virtual column metadata for the row-index column."""
        if self._index_column is not None:
            return self._index_column

        max_row_index = max(self.row_count - 1, 0)
        content_width = len(str(max_row_index))
        self._index_column = Column("#", content_width=content_width)

        return self._index_column

    @property
    def _index_column_width(self) -> int:
        """The render width of the column containing row indices."""
        return (
            self._get_column_render_width(self.index_column)
            if self.show_row_index
            else 0
        )

    def get_cell_at(self, coordinate: Coordinate) -> pa.Scalar:
        """Return a cached Arrow scalar, requesting missing data asynchronously.

        Raises:
            CellNotExistError: The coordinate is outside the table bounds.
            CellNotLoadedError: The cell exists but its page has not arrived.
        """
        row, column = coordinate.row, coordinate.column

        if not self.is_valid_coordinate(coordinate):
            raise CellNotExistError(coordinate)

        value = self.data.peek(row, column)
        if value is None:
            self._queue_window(row)
            raise CellNotLoadedError(coordinate)
        return value

    def _queue_window(self, row: int) -> None:
        if not self.is_mounted:
            return
        if self._requested_window is not None:
            start, stop = self._requested_window
            if start <= row < stop:
                return
        if not self._missing_rows:
            self.call_next(self._request_missing_window)
        self._missing_rows.add(row)

    def _request_missing_window(self) -> None:
        if not self._missing_rows:
            return
        start = min(self._missing_rows)
        stop = min(self.row_count, max(self._missing_rows) + 1 + PREFETCH_ROWS)
        self._missing_rows.clear()
        self._requested_window = (start, stop)
        self.post_message(self.WindowRequested(self.data, start, stop))

    def accept_page(self, page: DataPage) -> None:
        """Publish a background read, invalidating loading placeholders."""
        sample_changed = self.data.add_page(page)
        self._requested_window = None
        if sample_changed:
            self._invalidate_layout(columns=True)
        else:
            self._clear_render_caches()
        self.refresh(layout=sample_changed)
        if page.start <= self.cursor_row < page.stop:
            self._highlight_cursor()

    def update_row_count(self, available: int, total: int | None) -> None:
        """Extend a streamed result without resetting navigation or loaded pages."""
        self.data.row_count = available
        self.data.total_rows = total
        self._index_column = None
        self._invalidate_layout()
        self.refresh(layout=True)

    def replace_table(self, table: pa.Table) -> None:
        """Replace data and invalidate every data-dependent layout and cache."""
        self.replace_data(TableData.from_table(table))

    def replace_data(self, data: TableData) -> None:
        """Switch data sources without retaining cells or requests from the old one."""
        self.data = data
        self._missing_rows.clear()
        self._requested_window = None
        self._index_column = None
        self._cell_renderable_cache.clear()
        self._invalidate_layout(columns=True)
        self._show_hover_cursor = False
        self.cursor_coordinate = Coordinate(0, 0)
        self.hover_coordinate = Coordinate(0, 0)
        if self.is_mounted:
            self.scroll_to(x=0, y=0, animate=False, force=True)
        self.refresh(layout=True)

    def _invalidate_layout(self, *, columns: bool = False) -> None:
        """Schedule dimension work, remeasuring data columns only when they changed."""
        if columns:
            self._columns = None
            self._column_offsets = None
        self._require_update_dimensions = True
        self._clear_render_caches()

    def _clear_render_caches(self) -> None:
        """Drop styled segments while retaining formatted immutable Arrow values."""
        self._update_count += 1
        self._cell_render_cache.clear()
        self._row_render_cache.clear()
        self._line_cache.clear()
        self._styles_cache.clear()

    def notify_style_update(self) -> None:
        """Refresh both formatted content and rendered output after theme changes."""
        super().notify_style_update()
        self._cell_renderable_cache.clear()
        self._clear_render_caches()
        self.refresh()

    def _on_resize(self, _: events.Resize) -> None:
        self._update_count += 1
        logger.debug(
            "App or widget has been resized. ArrowTable._update_count: %d",
            self._update_count,
        )

    def watch_show_cursor(self, show_cursor: bool) -> None:
        """Handle cursor visibility changes."""
        self._clear_render_caches()
        if show_cursor and self.cursor_type != "none":
            # When we re-enable the cursor, apply highlighting and
            # post the appropriate [Row|Column|Cell]Highlighted event.
            self._scroll_cursor_into_view(animate=False)
            self._highlight_cursor()

    def watch_show_header(self, show: bool) -> None:
        """Update table dimensions and rendering when header visibility changes."""
        width, height = self.virtual_size
        height_change = 1 if show else -1
        self.virtual_size = Size(width, height + height_change)
        self._scroll_cursor_into_view()
        self._clear_render_caches()

    def watch_show_row_index(self, show: bool) -> None:
        """Update table dimensions and rendering when row-index visibility changes."""
        width, height = self.virtual_size
        # At this point, `self.show_row_index` is already the new value.
        # If we are hiding the index column, `self._index_column_width` now returns 0,
        # but we still need the old visible width to subtract from `virtual_size`.
        column_width = self._get_column_render_width(self.index_column)
        width_change = column_width if show else -column_width
        self.virtual_size = Size(width + width_change, height)
        self._scroll_cursor_into_view()
        self._clear_render_caches()

    def watch_zebra_stripes(self) -> None:
        """Clear rendered rows when zebra striping changes."""
        self._clear_render_caches()

    def validate_cell_padding(self, cell_padding: int) -> int:
        """Clamp cell padding to a non-negative value."""
        return max(cell_padding, 0)

    def watch_cell_padding(self, old_padding: int, new_padding: int) -> None:
        """Update table dimensions and rendering when cell padding changes."""
        # A single side of a single cell will have its width changed by (new - old),
        # so the total width change is double that per column, times the number of
        # columns for the whole data table, including the index column.
        column_count = self.column_count + (1 if self.show_row_index else 0)
        width_change = 2 * (new_padding - old_padding) * column_count
        width, height = self.virtual_size
        self.virtual_size = Size(width + width_change, height)
        self._scroll_cursor_into_view()
        self._column_offsets = None
        self._clear_render_caches()

    def watch_hover_coordinate(self, old: Coordinate, value: Coordinate) -> None:
        """Refresh the old and new cells when hover position changes."""
        self.refresh_coordinate(old)
        self.refresh_coordinate(value)

    def watch_cursor_coordinate(
        self, old_coordinate: Coordinate, new_coordinate: Coordinate
    ) -> None:
        """Refresh cursor highlighting when the cursor coordinate changes."""
        if old_coordinate != new_coordinate:
            # Refresh the old and the new cell, and post the appropriate
            # message to tell users of the newly highlighted row/cell/column.
            if self.cursor_type == "cell":
                self.refresh_coordinate(old_coordinate)
                self._highlight_coordinate(new_coordinate)
            elif self.cursor_type == "row":
                # Row highlighting only depends on the row index. Horizontal cursor
                # movement within the same row doesn't change any rendered highlight,
                # so refreshing and reposting the highlighted row would only create
                # unnecessary repaint work.
                if old_coordinate.row != new_coordinate.row:
                    self.refresh_row(old_coordinate.row)
                    self._highlight_row(new_coordinate.row)
            elif self.cursor_type == "column":
                # Column highlighting only depends on the column index. Vertical cursor
                # movement within the same column doesn't change the visible highlight;
                # skipping it avoids an expensive full-column refresh path on large
                # tables.
                if old_coordinate.column != new_coordinate.column:
                    self.refresh_column(old_coordinate.column)
                    self._highlight_column(new_coordinate.column)

            if self._require_update_dimensions:
                self.call_after_refresh(self._scroll_cursor_into_view)
            else:
                self._scroll_cursor_into_view()

    def move_cursor(
        self,
        *,
        row: int | None = None,
        column: int | None = None,
        animate: bool = False,
        scroll: bool = True,
    ) -> None:
        """Move selected axes, clamp to data bounds, and optionally scroll into view."""
        cursor_row, cursor_column = self.cursor_coordinate
        if row is not None:
            cursor_row = row
        if column is not None:
            cursor_column = column
        destination = Coordinate(cursor_row, cursor_column)

        # Scroll the cursor after refresh to ensure the virtual height
        # (calculated in on_idle) has settled. If we tried to scroll before
        # the virtual size has been set, then it might fail if we added a bunch
        # of rows then tried to immediately move the cursor.
        # We do this before setting `cursor_coordinate` because its watcher will also
        # schedule a call to `_scroll_cursor_into_view` without optionally animating.
        if scroll:
            if self._require_update_dimensions:
                self.call_after_refresh(self._scroll_cursor_into_view, animate=animate)
            else:
                self._scroll_cursor_into_view(animate=animate)

        self.cursor_coordinate = destination

    def _highlight_coordinate(self, coordinate: Coordinate) -> None:
        """Apply highlighting to the cell at the coordinate, and post event."""
        self.refresh_coordinate(coordinate)
        try:
            cell_value = self.get_cell_at(coordinate)
        except CellNotExistError:
            # The cell may not exist e.g. when the table is cleared.
            # In that case, there's nothing for us to do here.
            return
        else:
            self.post_message(
                ArrowTable.CellHighlighted(self, cell_value, coordinate=coordinate)
            )

    def _highlight_row(self, row_index: int) -> None:
        """Apply highlighting to the row at the given index, and post event."""
        self.refresh_row(row_index)
        if self.is_valid_row_index(row_index):
            self.post_message(ArrowTable.RowHighlighted(self, row_index))

    def _highlight_column(self, column_index: int) -> None:
        """Apply highlighting to the column at the given index, and post event."""
        self.refresh_column(column_index)
        if self.is_valid_column_index(column_index):
            self.post_message(ArrowTable.ColumnHighlighted(self, column_index))

    def validate_cursor_coordinate(self, value: Coordinate) -> Coordinate:
        """Clamp cursor coordinates to the current table bounds."""
        return self._clamp_cursor_coordinate(value)

    def _clamp_cursor_coordinate(self, coordinate: Coordinate) -> Coordinate:
        """Clamp a coordinate such that it falls within the boundaries of the table."""
        # Empty table: no valid cell exists. Return Coordinate(0, 0) as a sentinel —
        # downstream paths (get_cell_at / _highlight_coordinate / _post_selected_message)
        # already guard with is_valid_coordinate / row_count == 0, so an out-of-bounds
        # cursor here is harmless. Avoids clamp(value, 0, -1) which has no contract.
        if self.row_count == 0 or self.column_count == 0:
            return Coordinate(0, 0)

        row, column = coordinate
        row = clamp(row, 0, self.row_count - 1)
        column = clamp(column, 0, self.column_count - 1)
        return Coordinate(row, column)

    def watch_cursor_type(self, old: str, new: str) -> None:
        """Refresh cursor highlighting when the cursor mode changes."""
        self._set_hover_cursor(False)
        if self.show_cursor:
            self._highlight_cursor()

        # Refresh cells that were previously impacted by the cursor
        # but may no longer be.
        if old == "cell":
            self.refresh_coordinate(self.cursor_coordinate)
        elif old == "row":
            row_index, _ = self.cursor_coordinate
            self.refresh_row(row_index)
        elif old == "column":
            _, column_index = self.cursor_coordinate
            self.refresh_column(column_index)

        self._scroll_cursor_into_view()

    def _highlight_cursor(self) -> None:
        """Apply highlighting and post the message for the active cursor target."""
        row_index, column_index = self.cursor_coordinate
        cursor_type = self.cursor_type
        # Apply the highlighting to the newly relevant cells
        if cursor_type == "cell":
            self._highlight_coordinate(self.cursor_coordinate)
        elif cursor_type == "row":
            self._highlight_row(row_index)
        elif cursor_type == "column":
            self._highlight_column(column_index)

    def _update_dimensions(self) -> None:
        """Called to recalculate the virtual (scrollable) size."""
        total_width = self._get_column_offsets()[-1] + self._index_column_width
        header_lines = 1 if self.show_header else 0
        self.virtual_size = Size(total_width, self.row_count + header_lines)

    def _get_cell_region(self, coordinate: Coordinate) -> Region:
        """Get the region of the cell at the given spatial coordinate."""
        if not self.is_valid_coordinate(coordinate):
            return Region(0, 0, 0, 0)

        row_index, column_index = coordinate

        # The x-coordinate of a cell is the sum of widths of the data cells to the left
        # plus the width of the render width of the longest row label.
        x = self._get_column_offsets()[column_index] + self._index_column_width
        width = self._get_column_render_width(self.columns[column_index])
        height = 1  # The height of the row.
        y = row_index + (1 if self.show_header else 0)
        return Region(x, y, width, height)

    def _get_row_region(self, row_index: int) -> Region:
        """Get the region of the row at the given index."""
        if not self.is_valid_row_index(row_index):
            return Region(0, 0, 0, 0)

        row_width = self._get_column_offsets()[-1] + self._index_column_width
        y = row_index + (1 if self.show_header else 0)
        return Region(0, y, row_width, 1)  # The height of the row is 1.

    def _get_column_region(self, column_index: int) -> Region:
        """Get the region of the column at the given index."""
        if not self.is_valid_column_index(column_index):
            return Region(0, 0, 0, 0)

        x = self._get_column_offsets()[column_index] + self._index_column_width
        width = self._get_column_render_width(self.columns[column_index])
        header_height = 1 if self.show_header else 0
        height = self._total_row_height + header_height
        return Region(x, 0, width, height)

    async def _on_idle(self, event: events.Idle) -> None:
        """Coalesce pending dimension changes into one recalculation before repaint."""
        _ = event

        if self._require_update_dimensions:
            self._require_update_dimensions = False
            self._update_dimensions()

    def refresh_coordinate(self, coordinate: Coordinate) -> Self:
        """Refresh a visible data cell and return the table."""
        if not self.is_valid_coordinate(coordinate):
            return self
        region = self._get_cell_region(coordinate)
        self._refresh_region(region)
        return self

    def refresh_row(self, row_index: int) -> Self:
        """Refresh the visible part of a data row and return the table."""
        if not self.is_valid_row_index(row_index):
            return self

        region = self._get_row_region(row_index)
        self._refresh_region(region)
        return self

    def refresh_column(self, column_index: int) -> Self:
        """Refresh the visible part of a data column and return the table."""
        if not self.is_valid_column_index(column_index):
            return self

        region = self._get_column_region(column_index)
        self._refresh_region(region)
        return self

    def _refresh_region(self, region: Region) -> Self:
        """Refresh the visible intersection of a region in virtual table coordinates."""
        # Refresh regions are expressed in virtual table coordinates. Column refreshes
        # can cover the full table height, and after scrolling that would translate
        # into a very large negative-y dirty region. Clip to the visible window first
        # so Textual only receives the portion that can actually be repainted.
        visible_region = region.intersection(self.window_region)
        # Region is falsy when width or height is zero, i.e. nothing is visible.
        if not visible_region:
            return self

        self.refresh(visible_region.translate(-self.scroll_offset))
        return self

    def is_valid_row_index(self, row_index: int) -> bool:
        """Return whether the row index is within the data bounds."""
        return 0 <= row_index < self.row_count

    def is_valid_column_index(self, column_index: int) -> bool:
        """Return whether the column index is within the data bounds."""
        return 0 <= column_index < self.column_count

    def is_valid_coordinate(self, coordinate: Coordinate) -> bool:
        """Return whether the coordinate is within the data bounds."""
        row_index, column_index = coordinate
        return self.is_valid_row_index(row_index) and self.is_valid_column_index(
            column_index
        )

    def _normalize_cache_coordinate(
        self, coordinate: Coordinate, visible: bool
    ) -> Coordinate:
        """Keep only axes that affect this cursor mode; use -1 for hidden axes."""
        if not visible or self.cursor_type == "none":
            return Coordinate(-1, -1)
        if self.cursor_type == "row":
            return Coordinate(coordinate.row, -1)
        if self.cursor_type == "column":
            return Coordinate(-1, coordinate.column)
        return coordinate

    def _get_cell_renderable(self, row_index: int, column_index: int) -> Text:
        """Format a single accessed cell without touching off-screen columns."""
        coordinate = Coordinate(row_index, column_index)
        if (renderable := self._cell_renderable_cache.get(coordinate)) is not None:
            return renderable
        if row_index == self._header_row_index:
            renderable = Text(
                ""
                if column_index == self._index_column_index
                else self.columns[column_index].name
            )
        elif column_index == self._index_column_index:
            renderable = Text(str(row_index), style="dim")
        else:
            try:
                renderable = self._cell_formatter(self.get_cell_at(coordinate))
            except CellNotLoadedError:
                return Text("…", style="dim")
        self._cell_renderable_cache[coordinate] = renderable
        return renderable

    def _render_cell(
        self,
        row_index: int,
        column_index: int,
        base_style: Style,
        width: int,
        cursor: bool = False,
        hover: bool = False,
    ) -> list[Segment]:
        """Render one padded, ellipsized line with cell metadata and highlighting."""
        is_header_cell = row_index == self._header_row_index
        is_row_index_cell = column_index == self._index_column_index

        effective_cursor = cursor and self.show_cursor
        effective_hover = hover and self.show_cursor and self._show_hover_cursor
        cache_key = CellCacheKey(
            row_index,
            column_index,
            base_style,
            effective_cursor,
            effective_hover,
            self._update_count,
            self._pseudo_class_state,
        )

        # LRUCache records stats in get()/__getitem__, but `in` bypasses misses.
        # Use get() here so cell cache hit/miss stats stay accurate.
        if (segments := self._cell_render_cache.get(cache_key)) is not None:
            return segments

        try:
            console = self.app.console  # pyright: ignore
        except NoActiveAppError:
            console = Console()  # Use a fallback console
        base_style += Style.from_meta({"row": row_index, "column": column_index})

        cell = self._get_cell_renderable(row_index, column_index)

        component_style, post_style = self._get_styles_to_render_cell(
            is_header=is_header_cell or is_row_index_cell,
            hover=effective_hover,
            cursor=effective_cursor,
        )

        options = console.options.update_dimensions(width, 1).update(
            no_wrap=True, overflow="ellipsis"
        )

        segments = console.render_lines(
            Styled(
                Padding(cell, (0, self.cell_padding)),
                pre_style=base_style + component_style,
                post_style=post_style,
            ),
            options,
        )[0]  # Every table cell occupies exactly one terminal line.

        self._cell_render_cache[cache_key] = segments
        return segments

    def _get_styles_to_render_cell(
        self, *, is_header: bool, hover: bool, cursor: bool
    ) -> tuple[Style, Style]:
        """Resolve styles around cell content; cursor flags are already visibility-filtered."""
        component_style = Style()

        if hover:
            component_style += self.get_component_rich_style("arrowtable--hover")
            if is_header:
                # Apply subtle variation in style for the header/label (blue
                # background by default) rows and columns affected by the cursor, to
                # ensure we can still differentiate between the indices and the data.
                component_style += self.get_component_rich_style(
                    "arrowtable--header-hover"
                )

        if cursor:
            cursor_style = self.get_component_rich_style("arrowtable--cursor")
            component_style += cursor_style
            if is_header:
                component_style += self.get_component_rich_style(
                    "arrowtable--header-cursor"
                )

        post_foreground = (
            Style.from_color(color=component_style.color)
            if self.cursor_foreground_priority == "css"
            else Style.null()
        )
        post_background = (
            Style.from_color(bgcolor=component_style.bgcolor)
            if self.cursor_background_priority == "css"
            else Style.null()
        )

        return component_style, post_foreground + post_background

    def _render_row(
        self,
        row_index: int,
        base_style: Style,
        cursor_location: Coordinate,
        hover_location: Coordinate,
        start_column: int,
        stop_column: int,
    ) -> RenderedRow:
        """Render fixed index cells and data columns in [start_column, stop_column)."""
        cursor_type = self.cursor_type
        show_cursor = self.show_cursor

        normalized_cursor_coordinate = self._normalize_cache_coordinate(
            cursor_location, visible=show_cursor
        )
        normalized_hover_coordinate = self._normalize_cache_coordinate(
            hover_location, visible=show_cursor and self._show_hover_cursor
        )
        cache_key = RowCacheKey(
            row_index,
            base_style,
            normalized_cursor_coordinate,
            normalized_hover_coordinate,
            cursor_type,
            show_cursor,
            self._show_hover_cursor,
            self._update_count,
            self._pseudo_class_state,
            start_column,
            stop_column,
        )

        # LRUCache records stats in get()/__getitem__, but `in` bypasses misses.
        # Use get() here so row cache hit/miss stats stay accurate.
        if (row_pair := self._row_render_cache.get(cache_key)) is not None:
            return row_pair

        header_style = self.get_component_styles("arrowtable--header").rich_style

        # Keep the row-index column outside the horizontally scrollable segments.
        fixed_segments: list[Segment] = []

        if self.show_row_index:
            cell_location = Coordinate(row_index, self._index_column_index)
            fixed_segments = self._render_cell(
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
            )

        row_style = self._get_row_style(row_index, base_style)

        scrollable_segments: list[Segment] = []

        for column_index in range(start_column, stop_column):
            column = self.columns[column_index]
            cell_location = Coordinate(row_index, column_index)
            cell_segments = self._render_cell(
                row_index,
                column_index,
                row_style,
                width=self._get_column_render_width(column),
                cursor=self._should_highlight(
                    cursor_location, cell_location, cursor_type
                ),
                hover=self._should_highlight(
                    hover_location, cell_location, cursor_type
                ),
            )
            scrollable_segments.extend(cell_segments)

        row_pair = RenderedRow(fixed_segments, scrollable_segments)
        self._row_render_cache[cache_key] = row_pair
        return row_pair

    def _render_line(self, y: int, x1: int, x2: int, base_style: Style) -> Strip:
        """Crop a virtual table row into a viewport-width strip, keeping the index fixed."""
        width = self.size.width
        fixed_width = self._index_column_width
        visible_scrollable_width = max(0, width - fixed_width)
        start_column, stop_column = self._visible_column_range(
            x1, visible_scrollable_width
        )

        header_lines = 1 if self.show_header else 0
        row_index = (
            self._header_row_index if self.show_header and y == 0 else y - header_lines
        )
        if (
            not self.is_valid_row_index(row_index)
            and row_index != self._header_row_index
        ):
            return Strip.blank(width, base_style)

        normalized_cursor_coordinate = self._normalize_cache_coordinate(
            self.cursor_coordinate, visible=self.show_cursor
        )
        normalized_hover_coordinate = self._normalize_cache_coordinate(
            self.hover_coordinate, visible=self.show_cursor and self._show_hover_cursor
        )
        cache_key = LineCacheKey(
            y,
            x1,
            x2,
            width,
            normalized_cursor_coordinate,
            normalized_hover_coordinate,
            base_style,
            self.cursor_type,
            self._show_hover_cursor,
            self._update_count,
            self._pseudo_class_state,
        )

        # LRUCache records stats in get()/__getitem__, but `in` bypasses misses.
        # Use get() here so line cache hit/miss stats stay accurate.
        if (strip := self._line_cache.get(cache_key)) is not None:
            return strip

        row = self._render_row(
            row_index,
            base_style,
            cursor_location=self.cursor_coordinate,
            hover_location=self.hover_coordinate,
            start_column=start_column,
            stop_column=stop_column,
        )

        # Cropping is relative to the first rendered column, not the whole table.
        offsets = self._get_column_offsets()
        virtual_left = offsets[start_column] if start_column < len(offsets) else 0
        crop_start = max(0, x1 - virtual_left)
        crop_end = crop_start + visible_scrollable_width
        visible_cols_total = (
            (offsets[stop_column] - offsets[start_column])
            if stop_column > start_column
            else 0
        )

        segments = row.fixed + list(
            Strip(row.scrollable, visible_cols_total).crop(crop_start, crop_end)
        )
        strip = Strip(segments).adjust_cell_length(width, base_style).simplify()

        self._line_cache[cache_key] = strip
        return strip

    def render_lines(self, crop: Region) -> list[Strip]:
        """Capture focus/hover state before rendering viewport lines."""
        self._pseudo_class_state = self.get_pseudo_class_state()
        return super().render_lines(crop)

    def render_line(self, y: int) -> Strip:
        """Render the screen row at y, accounting for scrolling and the pinned header."""
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
        """Return whether the active cursor covers the target cell."""
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
        """Resolve header or zebra styles, falling back to the table style."""
        if row_index == self._header_row_index:
            return self.get_component_styles("arrowtable--header").rich_style

        if self.zebra_stripes:
            component_row_style = (
                "arrowtable--even-row" if row_index % 2 == 0 else "arrowtable--odd-row"
            )
            return self.get_component_styles(component_row_style).rich_style

        return base_style

    def _on_mouse_move(self, event: events.MouseMove) -> None:
        """Update the hover cursor from row and column metadata under the mouse."""
        self._set_hover_cursor(True)
        meta = event.style.meta
        if not meta:
            self._set_hover_cursor(False)
            return

        if self.cursor_type != "row" and meta.get("out_of_bounds", False):
            self._set_hover_cursor(False)
            return

        if self.show_cursor and self.cursor_type != "none":
            with contextlib.suppress(KeyError):
                self.hover_coordinate = Coordinate(meta["row"], meta["column"])

    def _on_leave(self, event: events.Leave) -> None:
        _ = event

        self._set_hover_cursor(False)

    def _get_fixed_offset(self) -> Spacing:
        """Calculate the "fixed offset".

        Fixed offset is the space to the top and left that is occupied by fixed rows
        and columns respectively. Fixed rows and columns are rows and columns that do
        not participate in scrolling.
        """
        top = 1 if self.show_header else 0
        left = self._index_column_width
        return Spacing(top, 0, 0, left)

    def _scroll_cursor_into_view(self, animate: bool = False) -> None:
        """Scroll handler to ensure cursor visible.

        When the cursor is at a boundary of the ArrowTable and moves out
        of view, this method handles scrolling to ensure it remains visible.
        """
        fixed_offset = self._get_fixed_offset()
        top, _, _, left = fixed_offset

        if self.cursor_type == "row":
            x, y, width, height = self._get_row_region(self.cursor_row)
            region = Region(int(self.scroll_x) + left, y, width - left, height)
        elif self.cursor_type == "column":
            x, y, width, height = self._get_column_region(self.cursor_column)
            region = Region(x, int(self.scroll_y) + top, width, height - top)
        else:
            region = self._get_cell_region(self.cursor_coordinate)

        self.scroll_to_region(region, animate=animate, spacing=fixed_offset, force=True)

    def _set_hover_cursor(self, active: bool) -> None:
        """Set whether the hover cursor is visible or not.

        The hover cursor is the faint cursor you see when you hover the mouse cursor
        over a cell. Typically, when you interact with the keyboard, you want to
        switch the hover cursor off.

        Args:
            active: Display the hover cursor.
        """
        # Keyboard navigation repeatedly hides the hover cursor. If the state is
        # already unchanged, refreshing the hover row/column/cell cannot affect the
        # rendered output; in column mode it would still schedule a costly column
        # refresh, so return before touching render state.
        if self._show_hover_cursor == active:
            return

        self._show_hover_cursor = active
        cursor_type = self.cursor_type
        if cursor_type == "column":
            self.refresh_column(self.hover_column)
        elif cursor_type == "row":
            self.refresh_row(self.hover_row)
        elif cursor_type == "cell":
            self.refresh_coordinate(self.hover_coordinate)

    async def _on_click(self, event: events.Click) -> None:
        self._set_hover_cursor(True)
        meta = event.style.meta
        if "row" not in meta or "column" not in meta:
            return
        if self.cursor_type != "row" and meta.get("out_of_bounds", False):
            return

        row_index = meta["row"]
        column_index = meta["column"]
        is_header_click = self.show_header and row_index == -1
        is_row_index_click = self.show_row_index and column_index == -1
        if is_header_click:
            # Header clicks work even if cursor is off, and doesn't move the cursor.
            column = self.columns[column_index]
            self.post_message(
                ArrowTable.HeaderSelected(self, column_index, label=Text(column.name))
            )
        elif is_row_index_click:
            self.post_message(ArrowTable.RowIndexSelected(self, row_index))
        elif self.show_cursor and self.cursor_type != "none":
            # Only post selection events if there is a visible row/col/cell cursor.
            new_coordinate = Coordinate(row_index, column_index)
            highlight_click = new_coordinate == self.cursor_coordinate
            self.cursor_coordinate = new_coordinate
            if highlight_click:
                self._post_selected_message()
            self._scroll_cursor_into_view(animate=True)
            event.stop()

    def action_page_down(self) -> None:
        """Move the cursor one page down."""
        self._set_hover_cursor(False)
        if self.show_cursor and self.cursor_type in ("cell", "row"):
            height = self.scrollable_content_region.height - (
                1 if self.show_header else 0
            )

            # Determine how many rows constitutes a "page"
            row_index, _ = self.cursor_coordinate
            rows_to_move = min(height, self.row_count - 1 - row_index)

            target_row = row_index + rows_to_move
            self.scroll_relative(y=height, animate=False, force=True)
            self.move_cursor(row=target_row, scroll=False)
        else:
            super().action_page_down()

    def action_page_up(self) -> None:
        """Move the cursor one page up."""
        self._set_hover_cursor(False)
        if self.show_cursor and self.cursor_type in ("cell", "row"):
            height = self.scrollable_content_region.height - (
                1 if self.show_header else 0
            )

            # Determine how many rows constitutes a "page"
            row_index, _ = self.cursor_coordinate
            rows_to_move = min(height, row_index)

            target_row = row_index - rows_to_move
            self.scroll_relative(y=-height, animate=False)
            self.move_cursor(row=target_row, scroll=False)
        else:
            super().action_page_up()

    def action_scroll_top(self) -> None:
        """Move the cursor and scroll to the top."""
        self._set_hover_cursor(False)
        cursor_type = self.cursor_type
        if self.show_cursor and (cursor_type == "cell" or cursor_type == "row"):
            _, column_index = self.cursor_coordinate
            self.cursor_coordinate = Coordinate(0, column_index)
        else:
            super().action_scroll_home()

    def action_scroll_bottom(self) -> None:
        """Move the cursor and scroll to the bottom."""
        self._set_hover_cursor(False)
        cursor_type = self.cursor_type
        if self.show_cursor and (cursor_type == "cell" or cursor_type == "row"):
            _, column_index = self.cursor_coordinate
            self.cursor_coordinate = Coordinate(self.row_count - 1, column_index)
        else:
            super().action_scroll_end()

    def action_scroll_home(self) -> None:
        """Move the cursor and scroll to the leftmost column."""
        self._set_hover_cursor(False)
        cursor_type = self.cursor_type
        if self.show_cursor and (cursor_type == "cell" or cursor_type == "column"):
            self.move_cursor(column=0)
        else:
            self.scroll_x = 0

    def action_scroll_end(self) -> None:
        """Move the cursor and scroll to the rightmost column."""
        self._set_hover_cursor(False)
        cursor_type = self.cursor_type
        if self.show_cursor and (cursor_type == "cell" or cursor_type == "column"):
            self.move_cursor(column=len(self.columns) - 1)
        else:
            self.scroll_x = self.max_scroll_x

    def action_cursor_up(self) -> None:
        """Move the cursor up or scroll up when cursor movement is disabled."""
        self._set_hover_cursor(False)
        cursor_type = self.cursor_type
        if self.show_cursor and (cursor_type == "cell" or cursor_type == "row"):
            self.cursor_coordinate = self.cursor_coordinate.up()
        else:
            # If the cursor doesn't move up (e.g. column cursor can't go up),
            # then ensure that we instead scroll the ArrowTable.
            super().action_scroll_up()

    def action_cursor_down(self) -> None:
        """Move the cursor down or scroll down when cursor movement is disabled."""
        self._set_hover_cursor(False)
        cursor_type = self.cursor_type
        if self.show_cursor and (cursor_type == "cell" or cursor_type == "row"):
            self.cursor_coordinate = self.cursor_coordinate.down()
        else:
            super().action_scroll_down()

    def action_cursor_right(self) -> None:
        """Move the cursor right or scroll right when cursor movement is disabled."""
        self._set_hover_cursor(False)
        cursor_type = self.cursor_type
        if self.show_cursor and (cursor_type == "cell" or cursor_type == "column"):
            self.cursor_coordinate = self.cursor_coordinate.right()
            self._scroll_cursor_into_view(animate=True)
        else:
            super().action_scroll_right()

    def action_cursor_left(self) -> None:
        """Move the cursor left or scroll left when cursor movement is disabled."""
        self._set_hover_cursor(False)
        cursor_type = self.cursor_type
        if self.show_cursor and (cursor_type == "cell" or cursor_type == "column"):
            self.cursor_coordinate = self.cursor_coordinate.left()
            self._scroll_cursor_into_view(animate=True)
        else:
            super().action_scroll_left()

    def action_select_cursor(self) -> None:
        """Select the row, column, or cell currently under the cursor."""
        self._set_hover_cursor(False)
        if self.show_cursor and self.cursor_type != "none":
            self._post_selected_message()

    def _post_selected_message(self) -> None:
        """Post the appropriate message for a selection based on the `cursor_type`."""
        cursor_coordinate = self.cursor_coordinate
        cursor_type = self.cursor_type
        if self.row_count == 0:
            return
        if cursor_type == "cell":
            try:
                value = self.get_cell_at(cursor_coordinate)
            except CellNotExistError:
                return
            self.post_message(
                ArrowTable.CellSelected(self, value, coordinate=cursor_coordinate)
            )
        elif cursor_type == "row":
            row_index, _ = cursor_coordinate
            self.post_message(ArrowTable.RowSelected(self, row_index))
        elif cursor_type == "column":
            _, column_index = cursor_coordinate
            self.post_message(ArrowTable.ColumnSelected(self, column_index))
