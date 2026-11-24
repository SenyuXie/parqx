"""The explicit starting point for browsing a table value."""

from dataclasses import dataclass

import pyarrow as pa


@dataclass(frozen=True, eq=False)
class BrowseRoot:
    """One explicit cell or row selection in a table's data generation."""

    generation: int
    row: int
    column: int | None
    name: str
    value: pa.Scalar | None

    @property
    def label(self) -> str:
        """Identify the selected row and optional cell without needing its data."""
        label = f"Row {self.row}"
        return label if self.column is None else f"{label} · {self.name}"
