"""Shared test fixtures."""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


@pytest.fixture
def small_parquet(tmp_path: Path) -> Path:
    """Write a tiny well-formed parquet file and return its path.

    Generated in-line rather than read from `data/` so tests stay hermetic
    and independent of the source tree layout.
    """
    path = tmp_path / "smoke.parquet"
    table = pa.table(
        {
            "id": pa.array([1, 2, 3, 4, 5], type=pa.int64()),
            "name": ["alice", "bob", "carol", "dave", "eve"],
            "score": [1.5, 2.5, 3.5, 4.5, 5.5],
        }
    )
    pq.write_table(table, path)  # type: ignore
    return path
