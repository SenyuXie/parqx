import pyarrow as pa
import pytest
from rich.cells import cell_len

from parqx.tui.cell_formatter import CellFormatter


def test_rejects_inline_limit_smaller_than_binary_prefix() -> None:
    with pytest.raises(ValueError, match="inline_limit must be at least 2"):
        CellFormatter(inline_limit=1)


def test_rejects_negative_max_nested_depth() -> None:
    with pytest.raises(ValueError, match="max_nested_depth must be non-negative"):
        CellFormatter(inline_limit=32, max_nested_depth=-1)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(b"", "0x"), (b"\x00", "0x00"), (b"\x00\x01\x02", "0x000102")],
)
def test_format_binary_reads_complete_value_when_it_fits(
    value: bytes, expected: str
) -> None:
    formatter = CellFormatter(inline_limit=48)

    result = formatter(pa.scalar(value))

    assert result.plain == expected


def test_format_binary_reads_only_enough_to_overflow_inline_limit() -> None:
    formatter = CellFormatter(inline_limit=6)
    scalar = pa.scalar(bytes(range(32)))

    result = formatter(scalar)

    assert result.plain == "0x000102"
    assert cell_len(result.plain) > formatter.inline_limit


def test_format_duration() -> None:
    formatter = CellFormatter(inline_limit=32)
    scalar = pa.scalar(12345, type=pa.duration("us"))

    result = formatter(scalar)

    assert result.plain == "12345us"


def test_format_month_day_nano_interval() -> None:
    formatter = CellFormatter(inline_limit=64)
    scalar = pa.scalar((1, 2, 3), type=pa.month_day_nano_interval())

    result = formatter(scalar)

    assert result.plain == "MonthDayNano(months=1, days=2, nanoseconds=3)"


def test_format_fallback_escapes_top_level_string() -> None:
    formatter = CellFormatter(inline_limit=32)
    dictionary_type = pa.dictionary(pa.int8(), pa.string())
    scalar = pa.array(["line\r\nbreak\t[red]"], type=dictionary_type)[0]

    result = formatter(scalar)

    assert result.plain == r"line\nbreak\t[red]"


def test_format_fallback_quotes_nested_string() -> None:
    formatter = CellFormatter(inline_limit=32)
    dictionary_type = pa.dictionary(pa.int8(), pa.string())
    struct_type = pa.struct({"payload": dictionary_type})
    scalar = pa.array([{"payload": 'say "hello"\n'}], type=struct_type)[0]

    result = formatter(scalar)

    assert result.plain == '{payload: "say \\"hello\\"\\n"}'


def test_format_fallback_formats_non_string_value() -> None:
    formatter = CellFormatter(inline_limit=32)
    dictionary_type = pa.dictionary(pa.int8(), pa.int64())
    scalar = pa.array([123], type=dictionary_type)[0]

    result = formatter(scalar)

    assert result.plain == "123"


def test_dictionary_scalar_uses_fallback_formatter() -> None:
    formatter = CellFormatter(inline_limit=32)
    dictionary_type = pa.dictionary(pa.int8(), pa.string())
    scalar = pa.array(["line\nbreak"], type=dictionary_type)[0]

    result = formatter(scalar)

    assert result.plain == r"line\nbreak"
