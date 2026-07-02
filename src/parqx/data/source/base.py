"""The data source protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import pyarrow as pa


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    """Column metadata."""

    name: str
    """Column name exposed by the source."""
    arrow_type: pa.DataType
    """Arrow data type of values in this column."""


@runtime_checkable
class TableSource(Protocol):
    """Protocol between the TUI layer and the data layer."""

    @property
    def row_count(self) -> int:
        """Total number of rows exposed by this source."""
        ...

    @property
    def column_count(self) -> int:
        """Total number of columns exposed by this source."""
        ...

    @property
    def columns(self) -> tuple[ColumnInfo, ...]:
        """Column metadata in source order. Stable across calls."""
        ...

    def get_cell_at(self, row: int, column: int) -> pa.Scalar:
        """Return the value at (row, column).

        Args:
            row: Zero-based row index of the value to retrieve.
            column: Zero-based column index of the value to retrieve.

        Raises:
            IndexError: If row or column is out of range.
        """
        ...

    def close(self) -> None:
        """Release file handles and caches. Idempotent."""
        ...
