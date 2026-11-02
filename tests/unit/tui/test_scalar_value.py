"""Complete scalar content, independent of the table preview formatter."""

from decimal import Decimal
from math import copysign, isnan

import pyarrow as pa
import pytest

from parqx.config import ParqxConfig
from parqx.query.engine import execute_query
from parqx.tui.scalar_value import read_scalar


@pytest.mark.parametrize(
    ("type_name", "value", "expected"),
    [
        ("int8", -128, "-128"),
        ("int16", -32768, "-32768"),
        ("int32", -2147483648, "-2147483648"),
        ("int64", -9223372036854775808, "-9223372036854775808"),
        ("uint8", 255, "255"),
        ("uint16", 65535, "65535"),
        ("uint32", 4294967295, "4294967295"),
        ("uint64", 18446744073709551615, "18446744073709551615"),
        ("bool", True, "true"),
        ("bool", False, "false"),
    ],
)
def test_scalar_digits_and_booleans_are_complete(
    type_name: str, value: int | bool, expected: str
) -> None:
    result = read_scalar(pa.scalar(value, type=pa.type_for_alias(type_name)))
    assert result.text == expected
    assert result.metadata == type_name
    assert result.info.startswith("Complete value")


@pytest.mark.parametrize(
    ("data_type", "text"),
    [
        (pa.decimal32(9, 4), "12345.6700"),
        (pa.decimal64(18, 6), "-123456789012.340000"),
        (pa.decimal128(38, 8), "123456789012345678901234567890.12340000"),
        (
            pa.decimal256(76, 12),
            "1234567890123456789012345678901234567890123456789012345678901234.567890120000",
        ),
        (pa.decimal128(8, -3), "12345000"),
        (pa.decimal128(6, 4), "0.0000"),
    ],
)
def test_decimal_reading_keeps_precision_and_scale(
    data_type: pa.DataType, text: str
) -> None:
    result = read_scalar(pa.scalar(Decimal(text), type=data_type))
    assert result.text == text
    assert result.metadata == str(data_type)
    assert result.role == "number"
    assert result.info.startswith("Complete value")


@pytest.mark.parametrize("type_name", ["float16", "float32", "float64"])
@pytest.mark.parametrize("value", [0.1, -0.0, 0.0, 1.23456789, 1e-7])
def test_finite_float_text_round_trips_to_the_supplied_type(
    type_name: str, value: float
) -> None:
    data_type = pa.type_for_alias(type_name)
    scalar = pa.scalar(value, type=data_type)
    result = read_scalar(scalar)
    restored = pa.scalar(float(result.text), type=data_type).as_py()
    assert restored == scalar.as_py()
    assert copysign(1, restored) == copysign(1, scalar.as_py())
    assert result.metadata == str(data_type)
    assert result.role == "number"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(float("nan"), "NaN"), (float("inf"), "+Infinity"), (float("-inf"), "-Infinity")],
)
def test_special_floats_are_explicit(value: float, expected: str) -> None:
    result = read_scalar(pa.scalar(value))
    assert result.text == expected
    assert result.info.startswith("Complete value")
    assert isnan(float(result.text)) if isnan(value) else float(result.text) == value


def test_string_content_is_literal_and_not_a_table_preview() -> None:
    text = '  [bold]中文👋 e\u0301[/bold]\n{"key":true}\n' + "wide words " * 500
    result = read_scalar(pa.scalar(text))
    assert result.text == text
    assert "string" in result.metadata
    assert "characters" in result.metadata


@pytest.mark.parametrize("value", [pa.scalar(None), pa.scalar(None, pa.string())])
def test_null_and_empty_text_have_distinct_reading_states(value: pa.Scalar) -> None:
    null = read_scalar(value)
    empty = read_scalar(pa.scalar(""))
    literal = read_scalar(pa.scalar("null"))
    assert null.text == "null"
    assert null.info == "Null value"
    assert empty.text == '""'
    assert empty.info == "Empty string\n0 characters"
    assert literal.text == "null"
    assert literal.info.startswith("Complete value")
    assert "4 characters" in literal.metadata


@pytest.mark.parametrize(
    "value",
    [
        pa.scalar([1, 2]),
        pa.scalar({"x": 1}),
        pa.scalar([1, 2], type=pa.list_view(pa.int64())),
    ],
)
def test_non_leaf_and_unknown_types_are_explicitly_unsupported(
    value: pa.Scalar,
) -> None:
    result = read_scalar(value)
    assert result.metadata == str(value.type)
    assert "Unsupported" in result.text
    assert "Complete value" not in result.info


@pytest.mark.parametrize(
    ("data_type", "raw", "expected"),
    [
        (pa.date32(), 19723, "2024-01-01"),
        (pa.date64(), 1704067200000, "2024-01-01"),
        (pa.time32("s"), 3723, "01:02:03"),
        (pa.time32("ms"), 3723123, "01:02:03.123"),
        (pa.time64("us"), 3723123456, "01:02:03.123456"),
        (pa.time64("ns"), 3723123456789, "01:02:03.123456789"),
        (pa.timestamp("s"), 123, "1970-01-01 00:02:03"),
        (pa.timestamp("ms"), 123456, "1970-01-01 00:02:03.456"),
        (pa.timestamp("us"), 123456789, "1970-01-01 00:02:03.456789"),
        (pa.timestamp("ns"), 123456789012, "1970-01-01 00:02:03.456789012"),
        (pa.timestamp("ns"), -1, "1969-12-31 23:59:59.999999999"),
        (pa.timestamp("ns"), 9223372036854775807, "2262-04-11 23:47:16.854775807"),
        (pa.timestamp("ns", "UTC"), 123456789, "1970-01-01 00:00:00.123456789Z"),
        (
            pa.timestamp("ns", "Asia/Shanghai"),
            123456789,
            "1970-01-01 08:00:00.123456789+0800",
        ),
        (
            pa.timestamp("ns", "America/New_York"),
            123456789,
            "1969-12-31 19:00:00.123456789-0500",
        ),
    ],
)
def test_temporal_text_keeps_fractions_and_timezone(
    data_type: pa.DataType, raw: int, expected: str
) -> None:
    result = read_scalar(pa.scalar(raw, type=data_type))
    assert result.text == expected
    assert result.metadata == str(data_type)
    assert result.info.startswith("Complete value")


@pytest.mark.parametrize(
    ("data_type", "expected"),
    [
        (pa.duration("s"), "-123456789 s"),
        (pa.duration("ms"), "-123456789 ms"),
        (pa.duration("us"), "-123456789 us"),
        (pa.duration("ns"), "-123456789 ns"),
    ],
)
def test_duration_keeps_raw_units(data_type: pa.DataType, expected: str) -> None:
    result = read_scalar(pa.scalar(-123456789, type=data_type))
    assert result.text == expected
    assert result.metadata == str(data_type)


def test_interval_keeps_independent_signed_components() -> None:
    result = read_scalar(
        pa.scalar((14, -2, 3000000001), type=pa.month_day_nano_interval())
    )
    assert result.text == "14 months, -2 days, 3000000001 nanoseconds"
    assert result.metadata == "month_day_nano_interval"


@pytest.mark.parametrize(
    "data_type", [pa.string(), pa.large_string(), pa.string_view()]
)
@pytest.mark.parametrize(
    "json_extension", [False, True], ids=["text", "json-extension"]
)
def test_string_families_and_json_extensions_remain_literal(
    data_type: pa.DataType, json_extension: bool
) -> None:
    if json_extension:
        data_type = pa.json_(data_type)
    text = '  [bold]中文👋 e\u0301[/bold]\r\n\t{"key": "quoted"}\n' + "words " * 100
    result = read_scalar(pa.scalar(text, type=data_type))
    assert result.text == text
    assert "characters" in result.metadata
    assert result.role == "string"
    assert result.info.startswith("Complete value")
    empty = read_scalar(pa.scalar("", type=data_type))
    null = read_scalar(pa.scalar(None, type=data_type))
    assert empty.text == '""'
    assert empty.info.startswith("Empty string")
    assert null.text == "null"
    assert null.info == "Null value"


@pytest.mark.parametrize("null_index", [False, True])
def test_dictionary_null_members_and_indices_are_typed_nulls(null_index: bool) -> None:
    scalar = pa.DictionaryArray.from_arrays(
        pa.array([None if null_index else 0], type=pa.int8()),
        pa.array([None], type=pa.large_string()),
    )[0]
    result = read_scalar(scalar)
    assert result.text == "null"
    assert result.info == "Null value"
    assert "dictionary" in result.metadata
    assert "large_string" in result.metadata


@pytest.mark.parametrize(
    ("values", "expected", "role"),
    [
        (
            pa.array([Decimal("123.4500")], type=pa.decimal128(10, 4)),
            "123.4500",
            "number",
        ),
        (
            pa.array(['{"a": 1}\n'], type=pa.json_(pa.large_string())),
            '{"a": 1}\n',
            "string",
        ),
    ],
)
def test_dictionary_dispatch_preserves_the_actual_leaf(
    values: pa.Array, expected: str, role: str
) -> None:
    scalar = pa.DictionaryArray.from_arrays(pa.array([0], type=pa.int8()), values)[0]
    result = read_scalar(scalar)
    assert result.text == expected
    assert result.role == role
    assert "dictionary" in result.metadata
    assert result.info.startswith("Complete value")


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("union_value(a := 18446744073709551615::UBIGINT)", "18446744073709551615"),
        ("union_value(b := 'plain text')::UNION(a INTEGER, b VARCHAR)", "plain text"),
        ("union_value(a := union_value(b := 12.3400::DECIMAL(10,4)))", "12.3400"),
        ("union_value(a := NULL::INTEGER)", "null"),
        ("NULL::UNION(a INTEGER, b VARCHAR)", "null"),
    ],
)
def test_union_reading_uses_the_active_leaf(expression: str, expected: str) -> None:
    scalar = execute_query(
        (), f"SELECT {expression} AS value", config=ParqxConfig()
    ).table.column(0)[0]
    result = read_scalar(scalar)
    assert result.text == expected
    assert "union" in result.metadata
    assert (
        result.info == "Null value"
        if expected == "null"
        else result.info.startswith("Complete value")
    )


def test_uuid_has_its_complete_readable_form() -> None:
    result = read_scalar(pa.scalar(bytes(range(16)), type=pa.uuid()))
    assert result.text == "00010203-0405-0607-0809-0a0b0c0d0e0f"
    assert "uuid" in result.metadata
    assert result.info.startswith("Complete value")


@pytest.mark.parametrize(
    "data_type", [pa.binary(), pa.large_binary(), pa.binary(6), pa.binary_view()]
)
def test_binary_families_show_offsets_hex_and_printable_bytes(
    data_type: pa.DataType,
) -> None:
    result = read_scalar(pa.scalar(b"\x00A~\xff\nZ", type=data_type), width=29)
    assert result.text == "00000000  00 41 7e ff  |.A~.|\n00000004  0a 5a        |.Z  |"
    assert result.metadata == f"{data_type} · 6 bytes"
    assert result.info.startswith("Complete value")
    null = read_scalar(pa.scalar(None, type=data_type))
    assert null.text == "null"
    assert null.info == "Null value"


@pytest.mark.parametrize(
    "data_type", [pa.binary(), pa.large_binary(), pa.binary(0), pa.binary_view()]
)
def test_empty_binary_is_distinct_from_null(data_type: pa.DataType) -> None:
    result = read_scalar(pa.scalar(b"", type=data_type))
    assert result.text == "0x"
    assert result.metadata == f"{data_type} · 0 bytes"
    assert result.info == "Empty binary\n0 bytes"


@pytest.mark.parametrize(("width", "line_count"), [(29, 65), (45, 33), (77, 17)])
def test_binary_layout_adapts_without_losing_any_bytes(
    width: int, line_count: int
) -> None:
    data = bytes(range(256)) + b"END"
    result = read_scalar(pa.scalar(data), width=width)
    lines = result.text.splitlines()
    assert len(lines) == line_count
    assert all(len(line) <= width for line in lines)
    decoded = bytearray()
    for line in lines:
        assert int(line[:8], 16) == len(decoded)
        hex_bytes, _, _ = line[10:].partition("  |")
        decoded.extend(bytes.fromhex(hex_bytes))
    assert decoded == data
