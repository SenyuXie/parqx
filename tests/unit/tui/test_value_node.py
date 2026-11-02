"""Arrow container shape and positional navigation at the value interface."""

import pyarrow as pa
import pytest

from parqx.config import ParqxConfig
from parqx.query.engine import execute_query
from parqx.tui.value_node import RowValue, ValueNode


@pytest.mark.parametrize(
    "data_type",
    [pa.list_(pa.int64()), pa.large_list(pa.int64()), pa.list_(pa.int64(), 2)],
)
def test_list_families_preserve_indices_and_nulls(data_type: pa.DataType) -> None:
    node = ValueNode(pa.scalar([7, 9], type=data_type))
    assert node.is_container
    assert node.child_count == 2
    assert node.delimiters == ("[", "]")
    name, child = node.child(1)
    assert name == "[1]"
    assert child.scalar.as_py() == 9
    assert "2 items" in node.metadata
    assert not ValueNode(pa.scalar(None, type=data_type)).is_container
    with pytest.raises(IndexError):
        node.child(2)


def test_duplicate_struct_and_row_names_use_positions() -> None:
    scalar = pa.array(
        [("first", 7)], type=pa.struct([("x", pa.string()), ("x", pa.int64())])
    )[0]
    row = RowValue((("x", pa.scalar("first")), ("x", pa.scalar(7))))
    for value in (scalar, row):
        node = ValueNode(value)
        assert node.child_count == 2
        assert node.child(0)[0] == node.child(1)[0] == "x"
        assert node.child(0)[1].scalar.as_py() == "first"
        assert node.child(1)[1].scalar.as_py() == 7


def test_map_entries_preserve_duplicate_nested_keys_and_values() -> None:
    value = pa.scalar(
        [({"id": 7}, ["east"]), ({"id": 7}, ["west"])],
        type=pa.map_(pa.struct({"id": pa.int64()}), pa.list_(pa.string())),
    )
    node = ValueNode(value)
    assert node.child_count == 2
    assert node.metadata == "map · 2 entries"
    assert node.delimiters == ("[", "]")
    name, second = node.child(1)
    assert name == "[1]"
    assert second.child_count == 2
    assert second.child(0)[0] == "key"
    assert second.child(1)[0] == "value"
    assert second.child(0)[1].child(0)[1].scalar.as_py() == 7
    assert second.child(1)[1].child(0)[1].scalar.as_py() == "west"
    assert node.child(0)[1].child(1)[1].child(0)[1].scalar.as_py() == "east"


def test_supported_wrappers_expose_container_shape() -> None:
    dictionary = pa.DictionaryArray.from_arrays(
        pa.array([0], type=pa.int8()), pa.array([{"items": [7, 8]}])
    )[0]
    union = execute_query(
        (),
        "SELECT union_value(nested := {'items': [7, 8]}) AS value",
        config=ParqxConfig(),
    ).table.column(0)[0]
    for scalar in (dictionary, union):
        node = ValueNode(scalar)
        assert node.is_container
        assert node.child(0)[0] == "items"
        assert node.child(0)[1].child(1)[1].scalar.as_py() == 8
        assert node.type_description == str(scalar.type)


def test_previews_show_leading_content_and_balanced_truncation() -> None:
    node = ValueNode(pa.scalar({"name": "Ada", "later": [1, 2, 3]}))
    assert node.preview(32).plain == '{ "name": "Ada", … }'
    entry = ValueNode(
        pa.scalar([(7, "east")], type=pa.map_(pa.int64(), pa.string()))
    ).child(0)[1]
    assert entry.preview(40).plain == '{ "key": 7, "value": "east" }'
    text = ValueNode(pa.scalar('中文 [bold] line\n"quote" ' * 20))
    nested = ValueNode(pa.scalar({"outer": [{"inner": [1, 2, 3]}]}))
    for width in (5, 12, 18, 30, 50):
        for value in (node, entry, text, nested):
            preview = value.preview(width)
            assert preview.cell_len <= width
            stack: list[str] = []
            quoted = escaped = False
            for character in preview.plain:
                if quoted:
                    if escaped:
                        escaped = False
                    elif character == "\\":
                        escaped = True
                    elif character == '"':
                        quoted = False
                elif character == '"':
                    quoted = True
                elif character in "{[":
                    stack.append(character)
                elif character in "}]":
                    assert stack.pop() == {"}": "{", "]": "["}[character]
            assert not stack
            assert not quoted
        assert "…" in text.preview(width).plain
        assert "\n" not in text.preview(width).plain


def test_long_field_labels_use_a_bounded_continuation_line() -> None:
    node = ValueNode(
        pa.scalar({"非常长的字段名称" * 10: "first value\nsecond line", "after": 3})
    )
    prompt = node.child_prompt(0, width=38, selected=True)
    assert prompt.plain.startswith("▸ ")
    assert '\n      "first value' in prompt.plain
    assert "\\n" in prompt.plain
    assert "…" in prompt.plain
    assert prompt.plain.endswith(",")
    assert all(line.cell_len <= 38 for line in prompt.split("\n"))


@pytest.mark.parametrize(
    ("data_type", "value"),
    [(pa.struct([]), {}), (pa.map_(pa.int64(), pa.string()), [])],
)
def test_empty_containers_and_typed_nulls_are_distinct(
    data_type: pa.DataType, value: object
) -> None:
    empty = ValueNode(pa.scalar(value, type=data_type))
    assert empty.is_container
    assert empty.child_count == 0
    assert empty.preview().plain in {"{}", "[]"}
    with pytest.raises(IndexError):
        empty.child(0)
    null = ValueNode(pa.scalar(None, type=data_type))
    assert not null.is_container
    assert null.preview().plain == "null"
    assert null.type_description == str(data_type)
