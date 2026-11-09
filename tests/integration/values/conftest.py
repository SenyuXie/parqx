"""Arrow and Parquet fixtures for value browsing."""

from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


@pytest.fixture
def large_values_parquet(tmp_path: Path) -> Path:
    """Large containers, logical lines, unbroken Unicode and binary content."""
    path = tmp_path / "large_values.parquet"
    # Cross multiple index batches and viewports. Million-character values
    # and allocation limits are exercised directly in test_value_document.
    text = ("中文👩🏽‍💻 e\u0301\t[bold]literal[/bold]\r\n" * 600) + "最后-END"
    pq.write_table(
        pa.table(
            {
                "items": [list(range(100_000))],
                "text": [text],
                "binary": [bytes(range(256)) * 32 + b"END"],
                "unbroken": ["中e\u0301👩🏽‍💻" * 4_000 + "END"],
            }
        ),
        path,
    )
    return path


@pytest.fixture
def paged_values_parquet(tmp_path: Path) -> Path:
    """File values spanning several pages, with nested and scrollable content."""
    path = tmp_path / "paged_values.parquet"
    pq.write_table(
        pa.table(
            {
                "id": range(600),
                "profile": [
                    {"name": f"person {i}", "active": True} for i in range(600)
                ],
                "note": [f"row {i}\n" + "read me\n" * 60 for i in range(600)],
            }
        ),
        path,
    )
    return path


@pytest.fixture
def encoded_scalar_table() -> pa.Table:
    """Arrow shapes that DuckDB may normalize before they reach the value reader."""
    return pa.table(
        {
            "encoded": pa.DictionaryArray.from_arrays(
                pa.array([0], pa.int8()),
                pa.array(
                    ['{"literal": "[bold]中文[/bold]"}\n'], pa.json_(pa.large_string())
                ),
            ),
            "precise": pa.array(
                [{"when": 123456789, "elapsed": -123456789}],
                pa.struct(
                    {"when": pa.timestamp("ns", "UTC"), "elapsed": pa.duration("ns")}
                ),
            ),
            "identifier": pa.array([bytes(range(16))], pa.uuid()),
            "bytes": pa.DictionaryArray.from_arrays(
                pa.array([0], pa.int8()), pa.array([b"\x00A~\xff\nZ"], pa.binary_view())
            ),
        }
    )


@pytest.fixture
def typed_scalars_parquet(tmp_path: Path) -> Path:
    """Precise scalars and ordinary binary content for both production readers."""
    path = tmp_path / "typed_scalars.parquet"
    pq.write_table(
        pa.table(
            {
                "amount": pa.array(
                    [Decimal("12345678901234567890.1200"), Decimal("0.0000"), None],
                    pa.decimal128(28, 4),
                ),
                "measured": [-0.0, float("inf"), None],
                "recorded": pa.array(
                    [1704067200123456789, -1, None], pa.timestamp("ns", "UTC")
                ),
                "payload": [bytes(range(256)) * 4 + b"END", b"", None],
                "note": [
                    '  [bold]literal[/bold] 中文 👋\n{"keep": "text"}\t  ',
                    "",
                    None,
                ],
            }
        ),
        path,
    )
    return path


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
