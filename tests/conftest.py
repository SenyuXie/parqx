"""Shared test fixtures."""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


@pytest.fixture
def deep_parquet(tmp_path: Path) -> Path:
    """Nested Unicode field labels that overflow the browser's breadcrumb strip."""
    path = tmp_path / "deep.parquet"
    value: object = '{"this remains": "plain text"}'
    for _ in range(5):
        value = {"skip": 0, "非常长的字段名称_" * 6: value}
    pq.write_table(pa.table({"payload": [value]}), path)
    return path


@pytest.fixture
def nested_parquet(tmp_path: Path) -> Path:
    """Ordered rows with nested values readable through DuckDB and Arrow."""
    path = tmp_path / "nested.parquet"
    pq.write_table(
        pa.table(
            {
                "id": [1, 2, 3],
                "profile": pa.array(
                    [
                        {"name": "Ada\n[bold]中文[/bold]", "active": True},
                        {"name": "Lin", "active": False},
                        None,
                    ],
                    type=pa.struct(
                        {"name": pa.string(), "active": pa.type_for_alias("bool")}
                    ),
                ),
                "items": pa.array(
                    [
                        [{"sku": "book", "quantity": 2}, {"sku": "pen", "quantity": 3}],
                        [],
                        None,
                    ],
                    type=pa.list_(
                        pa.struct({"sku": pa.string(), "quantity": pa.int64()})
                    ),
                ),
                "attributes": pa.array(
                    [[(7, ["east", "west"]), (9, ["north"])], [], None],
                    type=pa.map_(pa.int64(), pa.list_(pa.string())),
                ),
                **{f"extra_{i}": [i, i + 1, i + 2] for i in range(12)},
            }
        ),
        path,
    )
    return path


@pytest.fixture
def scalar_parquet(tmp_path: Path) -> Path:
    """Values whose full contents differ visibly from table previews."""
    path = tmp_path / "scalars.parquet"
    pq.write_table(
        pa.table(
            {
                "note": [
                    "[bold]literal[/bold] 中文 👋\n" + "read me\n" * 60,
                    "",
                    "null",
                ],
                "signed": pa.array([-9223372036854775808, 0, None], pa.int64()),
                "unsigned": pa.array([18446744073709551615, 0, None], pa.uint64()),
                "enabled": [True, False, None],
                "nested": [[1, 2], [], None],
            }
        ),
        path,
    )
    return path


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
    pq.write_table(table, path)
    return path
