import pyarrow as pa
import pytest
from rich.cells import cell_len

from parqx.tui.cell_formatter import CellFormatter
from parqx.tui.widgets.arrow_table import ArrowTable


def test_measure_short_binary_values() -> None:
    table = ArrowTable(pa.table({"x": [b"\x00\x01\x02", b"", None]}))

    assert table.columns[0].content_width == 8


@pytest.mark.parametrize(
    ("values", "data_type"),
    [
        ([0, 12345], pa.duration("us")),
        ([(0, 0, 0), (1, 2, 3)], pa.month_day_nano_interval()),
    ],
)
def test_measure_temporal_types_without_min_max_kernel(
    values: list[object], data_type: pa.DataType
) -> None:
    array = pa.array(values, type=data_type)
    table = ArrowTable(pa.table({"x": array}))
    formatter = CellFormatter(inline_limit=48)
    expected = max(cell_len(formatter(scalar).plain) for scalar in array)

    assert table.columns[0].content_width == expected


def test_extrema_measurement_includes_null_width() -> None:
    table = ArrowTable(pa.table({"x": pa.array([1, None], type=pa.int64())}))

    assert table.columns[0].content_width == 4


def test_sample_measurement_includes_unsampled_null_width() -> None:
    values: list[str | None] = ["x"] * 257
    values[128] = None
    table = ArrowTable(pa.table({"x": values}))

    assert table.columns[0].content_width == 4
