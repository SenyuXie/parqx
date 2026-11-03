"""Bounded rendering and complete positional access to large Arrow values."""

import tracemalloc

import pyarrow as pa
import pytest
from rich.cells import cell_len

from parqx.tui.value_document import ContainerDocument, ScalarDocument
from parqx.tui.value_node import RowValue, ValueNode


@pytest.mark.parametrize("kind", ["list", "map", "struct", "row"])
def test_container_navigation_does_not_build_all_child_previews(kind: str) -> None:
    count = 100_000
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
    assert "99" in node.child_prompt(count - 1, width=36, selected=True).plain


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
