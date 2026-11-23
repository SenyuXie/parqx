"""Measure Arrow columns and locate their visible cells in a table."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from itertools import accumulate
from math import ceil
from typing import NamedTuple, Self
from weakref import ReferenceType, WeakMethod, ref

import pyarrow as pa
from rich.cells import cell_len

from parqx.data.view import PageCache
from parqx.tui.cell_formatter import CellFormatter

_SAMPLE_ROWS = 256


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

    @classmethod
    def for_layout(
        cls, layout: ColumnLayout, name: str, content_width: int = 0
    ) -> Self:
        """Create a column that reports metadata edits to its layout."""
        column = cls(name, content_width=content_width)
        column._layout = ref(layout)
        return column

    def __getstate__(self) -> dict[str, object]:
        """Copy column metadata independently of the table that displays it."""
        state = vars(self).copy()
        state.pop("_layout", None)
        return state

    def __setattr__(self, name: str, value: object) -> None:
        """Invalidate cached positions after a public column attribute changes."""
        super().__setattr__(name, value)
        if name in {"name", "width", "content_width", "auto_width"}:
            owner: ReferenceType[ColumnLayout] | None = getattr(self, "_layout", None)
            if owner is not None and (layout := owner()) is not None:
                layout.column_changed(self)


class ColumnCrop(NamedTuple):
    """The data columns to render and the span to keep from their combined strip."""

    columns: range
    """Indices of the visible data columns."""
    strip_width: int
    """Full width of these columns before cropping, in cells."""
    start: int
    """First cell to keep within the strip."""
    end: int
    """One past the last cell to keep within the strip."""


def _sample_row_indices(total_rows: int) -> tuple[int, ...]:
    """Return deterministic row indices sampled from evenly spaced rows."""
    if total_rows <= _SAMPLE_ROWS:
        return tuple(range(total_rows))
    last = total_rows - 1
    return tuple(round(i * last / (_SAMPLE_ROWS - 1)) for i in range(_SAMPLE_ROWS))


class ColumnLayout:
    """Horizontal geometry shared by table rendering, cursor movement and scrolling."""

    def __init__(
        self,
        row_count: int,
        formatter: CellFormatter,
        *,
        on_change: WeakMethod[Callable[[], None]],
        padding: int,
        show_row_index: bool,
    ) -> None:
        """Set up row-index geometry without inspecting data columns."""
        self._on_change = on_change
        self._formatter = formatter
        self._padding = padding
        self.show_row_index = show_row_index
        self._offsets: tuple[int, ...] | None = None
        self._initialized = False
        self._measured = False
        self.columns: tuple[Column, ...] = ()
        self.index_column = Column.for_layout(
            self, "#", len(str(max(0, row_count - 1)))
        )

    def prepare_columns(self, data: pa.Table | PageCache) -> None:
        """Measure loaded data or use schema widths until a source page arrives."""
        if self._initialized:
            return
        if isinstance(data, pa.Table):
            self.columns = self._measure_columns(data)
            self._measured = True
        else:
            self.columns = tuple(
                Column.for_layout(self, field.name) for field in data.schema
            )
        self._initialized = True

    def column_changed(self, column: Column) -> None:
        """Discard cached geometry and request a redraw after a column edit."""
        # A caller may still hold schema metadata from before the first page arrived.
        if column is not self.index_column and not any(
            item is column for item in self.columns
        ):
            return
        self._offsets = None
        if (on_change := self._on_change()) is not None:
            on_change()

    def _measure_columns(self, table: pa.Table) -> tuple[Column, ...]:
        """Keep only width estimates from a bounded set of formatted scalars."""
        sample = _sample_row_indices(table.num_rows)
        return tuple(
            Column.for_layout(self, name, self._measure_content_width(column, sample))
            for name, column in zip(table.column_names, table.columns, strict=True)
        )

    def _measure_content_width(
        self, column: pa.ChunkedArray, sample: tuple[int, ...]
    ) -> int:
        """Estimate the 95th percentile display width, accounting for null values."""
        if pa.types.is_boolean(column.type):
            return 5  # false
        if pa.types.is_null(column.type):
            return 4  # null
        null_width = 4 if column.null_count else 0
        if not sample:
            return null_width
        # Scalar indexing avoids combining every chunk into a full-column copy.
        widths = sorted(
            cell_len(self._formatter(column[index]).plain) for index in sample
        )
        return max(null_width, widths[ceil(len(widths) * 0.95) - 1])

    def measure_page(self, table: pa.Table) -> bool:
        """Measure the first nonempty source page. Later pages keep the same widths.

        Returns:
            Whether the table should refresh its layout.
        """
        if self._measured or not table.num_rows:
            return False
        self.columns = self._measure_columns(table)
        self._measured = True
        self._offsets = None
        return True

    def set_padding(self, padding: int) -> None:
        """Apply horizontal cell padding to subsequent layout calculations."""
        self._padding = padding
        self._offsets = None

    def _render_width(self, column: Column) -> int:
        """Get the column's full width in terminal cells, including padding."""
        width = column.width
        if column.auto_width:
            width = min(
                max(cell_len(column.name), column.content_width),
                self._formatter.inline_limit,
            )
        return width + 2 * self._padding

    @property
    def _column_offsets(self) -> tuple[int, ...]:
        """Edges of the data columns, relative to the first data column."""
        if self._offsets is None:
            self._offsets = tuple(
                accumulate(map(self._render_width, self.columns), initial=0)
            )
        return self._offsets

    @property
    def index_width(self) -> int:
        """Width reserved for the fixed row index."""
        return self._render_width(self.index_column) if self.show_row_index else 0

    @property
    def width(self) -> int:
        """Width of the entire table, including its visible row index."""
        return self.index_width + self._column_offsets[-1]

    def get_column_bounds(self, index: int) -> tuple[int, int]:
        """Get the virtual x coordinate and width of a data column."""
        offsets = self._column_offsets
        return self.index_width + offsets[index], offsets[index + 1] - offsets[index]

    def crop(self, scroll_x: int, width: int) -> ColumnCrop:
        """Locate the scrollable cells visible beside the fixed row index.

        Args:
            scroll_x: Horizontal scroll offset, in cells.
            width: Width of the whole viewport, including the fixed row index.

        Returns:
            The data columns to render and crop offsets relative to their first cell.
        """
        available = max(0, width - self.index_width)
        offsets = self._column_offsets
        if not available or scroll_x >= offsets[-1]:
            return ColumnCrop(range(0), 0, 0, 0)
        first = max(0, bisect_right(offsets, scroll_x) - 1)
        stop = min(len(self.columns), bisect_left(offsets, scroll_x + available))
        start = max(0, scroll_x - offsets[first])
        return ColumnCrop(
            range(first, stop), offsets[stop] - offsets[first], start, start + available
        )
