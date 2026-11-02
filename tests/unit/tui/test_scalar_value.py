"""Complete scalar content, independent of the table preview formatter."""

import pyarrow as pa
import pytest

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
    [pa.scalar(1.25), pa.scalar(b"abc"), pa.scalar([1, 2]), pa.scalar({"x": 1})],
)
def test_types_outside_first_slice_are_explicitly_unsupported(value: pa.Scalar) -> None:
    result = read_scalar(value)
    assert result.metadata == str(value.type)
    assert "Unsupported" in result.text
    assert "Complete value" not in result.info
