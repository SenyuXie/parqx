"""Bounded rendering and complete positional access to large Arrow values."""

import tracemalloc

import pyarrow as pa
import pytest
from rich.cells import cell_len

from parqx.tui.values.document import ContainerDocument, ScalarDocument
from parqx.tui.values.node import RowValue, ValueNode


@pytest.mark.parametrize("kind", ["list", "map", "struct", "row"])
def test_container_navigation_does_not_build_all_child_previews(kind: str) -> None:
    count = 100_000
    value: pa.Scalar | RowValue
    if kind == "list":
        value = pa.scalar(list(range(count)))
    elif kind == "map":
        value = pa.scalar(
            [(str(i), i) for i in range(count)], type=pa.map_(pa.string(), pa.int64())
        )
    elif kind == "struct":
        value = pa.StructArray.from_arrays(
            [pa.array([i]) for i in range(count)],
            names=[f"field_{i}" for i in range(count)],
        )[0]
    else:
        value = RowValue(tuple((f"field_{i}", pa.scalar(i)) for i in range(count)))
    node = ValueNode(value)
    tracemalloc.start()
    document = ContainerDocument(node, width=36)
    while not document.complete:
        document.advance()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 3_000_000
    last_y = document.child_line(count - 1)
    assert document.child_at_line(last_y) == count - 1
    assert document.child_at_line(document.line_count - 1) is None
    assert "99" in node.child_prompt(count - 1, width=36).plain


def test_page_targets_use_wrapped_lines_and_stop_at_container_ends() -> None:
    value = pa.scalar(
        {"first": 1, "a_long_field_label_that_wraps": 2, "third": 3, "last": 4}
    )
    document = ContainerDocument(ValueNode(value), width=40)
    document.advance()
    # The middle field occupies lines two and three.
    assert document.page_target(0, 2) == 1
    assert document.page_target(2, -2) == 1
    assert document.page_target(1, 1) == 2
    assert document.page_target(1, 2, viewport_top=3) == 3
    assert document.page_target(0, -20) == 0
    assert document.page_target(2, 20) == 3


def test_long_unicode_is_indexed_incrementally_and_preserved() -> None:
    text = ("中文👩🏽‍💻 e\u0301\t[bold]literal[/bold]\r\n" * 40_000) + "最后-END"
    value = pa.scalar(text)
    tracemalloc.start()
    document = ScalarDocument(value, width=37)
    assert not document.complete
    document.advance()
    assert not document.complete
    assert document.line_count > 0
    while not document.complete:
        document.advance()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < len(text.encode("utf-8"))
    assert document.line(document.line_count - 1).endswith("最后-END")
    assert f"{len(text):,} characters" in document.metadata
    # The public raw slices preserve logical breaks, tabs and graphemes exactly.
    assert "".join(document.raw_line(i) for i in range(document.line_count)) == text


@pytest.mark.parametrize(
    "data_type", [pa.binary(), pa.large_binary(), pa.binary(6), pa.binary_view()]
)
def test_binary_families_show_offsets_hex_and_printable_bytes(
    data_type: pa.DataType,
) -> None:
    document = ScalarDocument(pa.scalar(b"\x00A~\xff\nZ", type=data_type), width=29)
    assert document.line_count == 2
    assert document.line(0) == "00000000  00 41 7e ff  |.A~.|"
    assert document.line(1) == "00000004  0a 5a        |.Z  |"
    assert document.metadata == f"{data_type} · 6 bytes"


@pytest.mark.parametrize(
    "data_type", [pa.binary(), pa.large_binary(), pa.binary(0), pa.binary_view()]
)
def test_empty_binary_is_distinct_from_null(data_type: pa.DataType) -> None:
    empty = ScalarDocument(pa.scalar(b"", type=data_type), width=29)
    empty.advance()
    assert empty.line(0) == "0x"
    assert empty.metadata == f"{data_type} · 0 bytes"

    null = ScalarDocument(pa.scalar(None, type=data_type), width=29)
    null.advance()
    assert null.line(0) == "null"
    assert null.metadata == str(data_type)


@pytest.mark.parametrize(("width", "line_count"), [(29, 65), (45, 33), (77, 17)])
def test_binary_layout_adapts_without_losing_any_bytes(
    width: int, line_count: int
) -> None:
    data = bytes(range(256)) + b"END"
    document = ScalarDocument(pa.scalar(data), width=width)
    assert document.line_count == line_count
    decoded = bytearray()
    for index in range(line_count):
        line = document.line(index)
        assert len(line) <= width
        assert int(line[:8], 16) == len(decoded)
        hex_bytes, _, _ = line[10:].partition("  |")
        decoded.extend(bytes.fromhex(hex_bytes))
    assert decoded == data


def test_binary_last_byte_is_available_without_formatting_preceding_bytes() -> None:
    data = bytes(range(256)) * 8192 + b"END"
    document = ScalarDocument(pa.scalar(data), width=29)
    assert document.complete
    assert document.bytes_per_line == 4
    assert document.line(document.line_count - 1).endswith("|END |")
    assert document.byte_offset(document.line_count - 1) == len(data) - 3


@pytest.mark.parametrize(
    "text",
    [
        "中\tEND",
        "e\u0301\tEND",
        "👩🏽‍💻\tEND",
        "a\r\nb\rc\n",
        "中" * 4095 + "👩🏽‍💻e\u0301END",
    ],
)
@pytest.mark.parametrize("width", [5, 17, 41])
def test_wrapping_preserves_graphemes_breaks_and_terminal_tab_stops(
    text: str, width: int
) -> None:
    document = ScalarDocument(pa.scalar(text), width=width)
    while not document.complete:
        document.advance()
    assert "".join(document.raw_line(i) for i in range(document.line_count)) == text
    assert all(cell_len(document.line(i)) <= width for i in range(document.line_count))
    if text == "中\tEND" and width > 5:
        assert document.line(0) == "中  END"
    if text == "a\r\nb\rc\n":
        assert [document.raw_line(i) for i in range(document.line_count)] == [
            "a\r\n",
            "b\r",
            "c\n",
            "",
        ]


def test_unbroken_unicode_larger_than_a_million_characters_and_reflow_anchor() -> None:
    text = "中e\u0301👩🏽‍💻" * 160_000 + "END"
    value = pa.scalar(text)
    narrow = ScalarDocument(value, width=29)
    while not narrow.complete:
        narrow.advance()
    assert narrow.line(narrow.line_count - 1).endswith("END")
    assert "".join(narrow.raw_line(i) for i in range(narrow.line_count)) == text
    original_line = narrow.line_count // 2
    anchor = narrow.byte_offset(original_line)
    wider = ScalarDocument(value, width=43)
    while not wider.complete:
        wider.advance()
    position = wider.line_at_byte(anchor)
    assert wider.byte_offset(position) <= anchor < wider.byte_offset(position + 1)


def test_terminal_controls_are_visible_literal_content() -> None:
    text = "prefix\x1b[2J\x07\x08\x00\x9b31mEND"
    document = ScalarDocument(pa.scalar(text), width=17)
    while not document.complete:
        document.advance()
    rendered = "".join(document.line(i) for i in range(document.line_count))
    assert rendered == r"prefix\x1b[2J\x07\x08\x00\x9b31mEND"
    assert all(cell_len(document.line(i)) <= 17 for i in range(document.line_count))
    assert "".join(document.raw_line(i) for i in range(document.line_count)) == text


@pytest.mark.parametrize("count", [10_000, 1_000_000])
def test_a_long_combining_sequence_yields_before_consuming_the_value(
    count: int,
) -> None:
    text = "e" + "\u0301" * count + "END"
    document = ScalarDocument(pa.scalar(text), width=37)
    document.advance()
    assert not document.complete
    assert document.line_count > 0
    assert len(document.raw_line(0)) < len(text)
    while not document.complete:
        document.advance()
    assert document.line(document.line_count - 1).endswith("END")
    assert "".join(document.raw_line(i) for i in range(document.line_count)) == text


@pytest.mark.parametrize("prefix", [0, 1, 35, 1000, 4090, 4096, 9000])
@pytest.mark.parametrize("width", [17, 37, 80])
def test_random_page_reads_preserve_shifted_and_repeated_combining_runs(
    prefix: int, width: int
) -> None:
    text = "x" * prefix + ("e" + "\u0301" * 10_000 + "MARKER\n") * 3 + "END"
    document = ScalarDocument(pa.scalar(text), width=width)
    while not document.complete:
        document.advance()
    # Read in reverse order too, so no access pattern can hide a regenerated seam.
    rows = [document.raw_line(i) for i in reversed(range(document.line_count))]
    assert "".join(reversed(rows)) == text
