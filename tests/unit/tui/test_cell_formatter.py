from typing import cast

import pyarrow as pa
import pytest
from rich.cells import cell_len

from parqx.query.engine import QueryControl, QuerySession
from parqx.tui.cell_formatter import CellFormatter


def test_rejects_inline_limit_smaller_than_binary_prefix() -> None:
    with pytest.raises(ValueError, match="inline_limit must be at least 2"):
        CellFormatter(inline_limit=1)


def test_rejects_negative_max_nested_depth() -> None:
    with pytest.raises(ValueError, match="max_nested_depth must be non-negative"):
        CellFormatter(inline_limit=32, max_nested_depth=-1)


@pytest.mark.parametrize(
    ("value", "expected"), [(b"", "0x"), (b"\x00\x01\x02", "0x000102")]
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


def test_format_dictionary_escapes_top_level_string() -> None:
    formatter = CellFormatter(inline_limit=32)
    dictionary_type = pa.dictionary(pa.int8(), pa.string())
    scalar = pa.array(["line\r\nbreak\t[red]"], type=dictionary_type)[0]

    result = formatter(scalar)

    assert result.plain == r"line\nbreak\t[red]"


def test_format_dictionary_quotes_nested_string() -> None:
    formatter = CellFormatter(inline_limit=32)
    dictionary_type = pa.dictionary(pa.int8(), pa.string())
    struct_type = pa.struct({"payload": dictionary_type})
    scalar = pa.array([{"payload": 'say "hello"\n'}], type=struct_type)[0]

    result = formatter(scalar)

    assert result.plain == '{payload: "say \\"hello\\"\\n"}'


def test_format_dictionary_formats_non_string_value() -> None:
    formatter = CellFormatter(inline_limit=32)
    dictionary_type = pa.dictionary(pa.int8(), pa.int64())
    scalar = pa.array([123], type=dictionary_type)[0]

    result = formatter(scalar)

    assert result.plain == "123"


@pytest.mark.parametrize(
    "data_type", [pa.string(), pa.large_string(), pa.string_view()]
)
@pytest.mark.parametrize("dictionary", [False, True])
def test_large_strings_only_format_a_prefix(
    data_type: pa.DataType, dictionary: bool
) -> None:
    values = pa.array(["x" * 1_000_000], type=data_type)
    if dictionary:
        values = pa.DictionaryArray.from_arrays(pa.array([0], type=pa.int8()), values)

    result = CellFormatter(inline_limit=16)(values[0])

    assert result.plain == "x" * 17


@pytest.mark.parametrize(
    "value",
    ["e" + "\u0301" * 100_000, "\u200d" * 100_000],
    ids=["combining-marks", "zero-width-joiners"],
)
@pytest.mark.parametrize("nested", [False, True])
def test_zero_width_characters_hit_a_byte_budget(value: str, nested: bool) -> None:
    scalar = (
        pa.array([{"value": value}], type=pa.struct({"value": pa.string()}))[0]
        if nested
        else pa.scalar(value)
    )
    formatter = CellFormatter(inline_limit=16)

    result = formatter(scalar)

    assert "…" in result.plain
    assert "\ufffd" not in result.plain
    assert len(result.plain) < 100
    assert cell_len(result.plain) <= formatter.inline_limit
    if nested:
        assert result.plain.endswith('…"}')


@pytest.mark.parametrize("value", ["", "hello", "e\u0301", "汉字🙂", "🙂" * 4])
@pytest.mark.parametrize("dictionary", [False, True])
def test_short_strings_keep_complete_unicode(value: str, dictionary: bool) -> None:
    values = pa.array([value])
    if dictionary:
        values = pa.DictionaryArray.from_arrays(pa.array([0], type=pa.int8()), values)

    assert CellFormatter(inline_limit=16)(values[0]).plain == value


def test_multibyte_prefix_never_splits_a_character() -> None:
    result = CellFormatter(inline_limit=16)(pa.scalar("🙂" * 1000))

    assert result.plain == "🙂" * 17
    assert cell_len(result.plain) > 16


@pytest.mark.parametrize("dictionary", [False, True])
def test_binary_views_use_the_bounded_binary_preview(dictionary: bool) -> None:
    values = pa.array([b"x" * 1_000_000], type=pa.binary_view())
    if dictionary:
        values = pa.DictionaryArray.from_arrays(pa.array([0], type=pa.int8()), values)

    result = CellFormatter(inline_limit=6)(values[0])

    assert result.plain == "0x787878"


@pytest.mark.parametrize("null_index", [False, True])
def test_dictionary_null_values_keep_null_formatting(null_index: bool) -> None:
    values = pa.DictionaryArray.from_arrays(
        pa.array([None if null_index else 0], type=pa.int8()),
        pa.array([None], type=pa.string()),
    )

    result = CellFormatter(inline_limit=16)(values[0])

    assert result.plain == "null"
    assert result.style == "dim italic magenta"


def test_dictionary_containers_preserve_temporal_precision_and_depth() -> None:
    values = pa.DictionaryArray.from_arrays(
        pa.array([0], type=pa.int8()),
        pa.array([[123456789]], type=pa.list_(pa.timestamp("ns", tz="UTC"))),
    )

    assert CellFormatter(inline_limit=64)(values[0]).plain == (
        "[1970-01-01 00:00:00.123456789Z]"
    )
    assert (
        CellFormatter(inline_limit=64, max_nested_depth=0)(values[0]).plain == "<list>"
    )


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("union_value(a := 42)", "42"),
        ("union_value(a := 'hello')", "hello"),
        ("union_value(b := 'hello')::UNION(a INTEGER, b VARCHAR)", "hello"),
        ("[union_value(a := 42)]", "[42]"),
        ("{'value': union_value(a := 'hello')}", '{value: "hello"}'),
        ("union_value(a := union_value(b := 42))", "42"),
    ],
)
def test_query_union_members_are_displayed(expression: str, expected: str) -> None:
    with QuerySession((), f"SELECT {expression} AS value", QueryControl()) as session:
        scalar = session.preview().table.column(0)[0]

    assert CellFormatter(inline_limit=32)(scalar).plain == expected


@pytest.mark.parametrize(
    "expression",
    ["union_value(a := NULL::INTEGER)", "NULL::UNION(a INTEGER, b VARCHAR)"],
)
def test_query_union_nulls_keep_null_formatting(expression: str) -> None:
    with QuerySession((), f"SELECT {expression} AS value", QueryControl()) as session:
        scalar = session.preview().table.column(0)[0]

    result = CellFormatter(inline_limit=16)(scalar)

    assert result.plain == "null"
    assert result.style == "dim italic magenta"


@pytest.mark.parametrize(
    ("member", "expected"),
    [
        ("repeat('x', 1000000)", "x" * 17),
        ("repeat('x', 1000000)::BLOB", "0x" + "78" * 8),
    ],
)
def test_query_union_large_members_use_bounded_previews(
    member: str, expected: str
) -> None:
    with QuerySession(
        (), f"SELECT union_value(a := {member}) AS value", QueryControl()
    ) as session:
        scalar = session.preview().table.column(0)[0]

    assert CellFormatter(inline_limit=16)(scalar).plain == expected


def test_query_union_containers_respect_nested_depth() -> None:
    with QuerySession(
        (), "SELECT union_value(a := [1, 2, 3]) AS value", QueryControl()
    ) as session:
        scalar = session.preview().table.column(0)[0]

    assert CellFormatter(inline_limit=32)(scalar).plain == "[1, 2, 3]"
    assert CellFormatter(inline_limit=32, max_nested_depth=0)(scalar).plain == "<list>"


@pytest.mark.parametrize(
    "storage_type", [pa.string(), pa.large_string(), pa.string_view()]
)
def test_json_values_use_bounded_string_formatting(storage_type: pa.DataType) -> None:
    data_type = pa.json_(storage_type)
    formatter = CellFormatter(inline_limit=16)
    short = '{"ok": true}'
    large = '{"data": "' + "x" * 1_000_000 + '"}'

    assert formatter(pa.scalar(short, type=data_type)).plain == short
    assert formatter(pa.scalar(large, type=data_type)).plain == large[:17]


@pytest.mark.parametrize("dictionary", [False, True])
def test_null_json_values_keep_null_formatting(dictionary: bool) -> None:
    values = pa.array([None], type=pa.json_())
    if dictionary:
        values = pa.DictionaryArray.from_arrays(pa.array([0], type=pa.int8()), values)

    result = CellFormatter(inline_limit=16)(values[0])

    assert result.plain == "null"
    assert result.style == "dim italic magenta"


def test_uuid_keeps_its_fixed_size_display() -> None:
    scalar = pa.scalar(bytes(range(16)), type=pa.uuid())

    assert CellFormatter(inline_limit=48)(scalar).plain == (
        "00010203-0405-0607-0809-0a0b0c0d0e0f"
    )


def test_unsupported_types_do_not_convert_their_values() -> None:
    class UnsupportedScalar:
        is_valid = True
        type = pa.list_view(pa.int64())

        def as_py(self) -> object:
            pytest.fail("Unsupported values must not be converted to Python")

    scalar = cast(pa.Scalar, UnsupportedScalar())

    assert CellFormatter(inline_limit=16)(scalar).plain == "<unsupported>"


@pytest.mark.parametrize(
    "name",
    ["x" * 100_000, "\n" * 100_000, "e" + "\u0301" * 100_000],
    ids=["identifier", "newlines", "combining-marks"],
)
def test_long_struct_field_names_are_bounded(name: str) -> None:
    scalar = pa.array([{name: 1}], type=pa.struct({name: pa.int64()}))[0]

    result = CellFormatter(inline_limit=16)(scalar)

    assert "…" in result.plain
    assert len(result.plain) < 100


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("count", "{count: 1}"),
        ("first name", '{"first name": 1}'),
        ('a"b', '{"a\\"b": 1}'),
    ],
)
def test_short_struct_field_names_keep_their_format(name: str, expected: str) -> None:
    scalar = pa.array([{name: 1}], type=pa.struct({name: pa.int64()}))[0]

    assert CellFormatter(inline_limit=32)(scalar).plain == expected
