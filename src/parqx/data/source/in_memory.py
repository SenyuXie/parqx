"""In-memory table source."""

from __future__ import annotations

from typing import TYPE_CHECKING

from parqx.data.source.base import ColumnInfo

if TYPE_CHECKING:
    import pyarrow as pa


class InMemorySource:
    """Table source backed by Arrow table loaded in memory."""

    def __init__(self, table: pa.Table) -> None:
        """Initialize the source with an Arrow table.

        Args:
            table: Table whose cells and schema are exposed by this source.
        """
        self._table = table
        self._columns = tuple(
            ColumnInfo(field.name, field.type) for field in table.schema
        )

    @property
    def row_count(self) -> int:
        """Total number of rows exposed by this source."""
        return self._table.num_rows

    @property
    def column_count(self) -> int:
        """Total number of columns exposed by this source."""
        return self._table.num_columns

    @property
    def columns(self) -> tuple[ColumnInfo, ...]:
        """Column metadata in source order. Stable across calls."""
        return self._columns

    def get_cell_at(self, row: int, column: int) -> pa.Scalar:
        """Get the value at (row, column).

        Args:
            row: Zero-based row index of the value to retrieve.
            column: Zero-based column index of the value to retrieve.

        Returns:
            TODO.

        Raises:
            IndexError: If row or column is out of range.
        """
        if not (0 <= row < self.row_count):
            raise IndexError(row)
        if not (0 <= column < self.column_count):
            raise IndexError(column)
        return self._table.column(column)[row]

    def close(self) -> None:
        """Release resources held by this source."""
