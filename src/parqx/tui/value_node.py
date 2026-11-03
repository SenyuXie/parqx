"""Positional access to available Arrow values and synthetic row values."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

import pyarrow as pa
from rich.cells import cell_len
from rich.style import Style, StyleType
from rich.text import Text

from parqx.tui.cell_formatter import CellFormatter
from parqx.tui.scalar_value import scalar_leaf

_COLORS: Mapping[str, StyleType] = {
    "key": "#91b8de",
    "string": "#b4cfaa",
    "number": "#e5c18a",
    "boolean": "#bba7dd",
    "muted": "#8493a5",
}


def _quoted(value: str, width: int, *, shortened: bool = False) -> str:
    """Fit an escaped label without losing its closing quote or truncation mark."""
    shortened |= len(value) > width * 4
    value = value[: width * 4]
    while True:
        rendered = json.dumps(value + ("…" if shortened else ""), ensure_ascii=False)
        if cell_len(rendered) <= width or not value:
            return rendered
        value = value[:-1]
        shortened = True


@dataclass(frozen=True)
class RowValue:
    """Retain a selected row's ordered fields without changing its schema."""

    fields: tuple[tuple[str, pa.Scalar], ...]


class ValueNode:
    """Expose direct children and type information without dictionary conversion."""

    def __init__(self, value: pa.Scalar | RowValue, *, map_entry: bool = False) -> None:
        """Wrap one available value, preserving its original Arrow type."""
        self.value = value
        self._entry = map_entry
        actual = value
        while (
            isinstance(actual, (pa.DictionaryScalar, pa.UnionScalar))
            and actual.is_valid
        ):
            member = actual.value
            if member is None:
                break
            actual = member
        self._actual = actual
        self.kind: Literal["struct", "list", "map"] | None = None
        if isinstance(actual, RowValue):
            self.kind = "struct"
        elif actual.is_valid:
            if isinstance(actual, pa.StructScalar):
                self.kind = "struct"
            elif isinstance(actual, pa.MapScalar):
                self.kind = "map"
            elif isinstance(
                actual, (pa.ListScalar, pa.LargeListScalar, pa.FixedSizeListScalar)
            ):
                self.kind = "list"

    @property
    def scalar(self) -> pa.Scalar:
        """Original Arrow scalar for dispatch through the shared leaf reader."""
        if isinstance(self.value, RowValue):
            raise TypeError("A row is an ordered collection of Arrow scalars")
        return self.value

    @property
    def is_container(self) -> bool:
        """Whether the value has a browsable structure, including an empty one."""
        return self.kind is not None

    @property
    def delimiters(self) -> tuple[str, str]:
        """JSON-like delimiters for the container's visible document."""
        return ("[", "]") if self.kind in {"list", "map"} else ("{", "}")

    @property
    def child_count(self) -> int:
        """Number of direct children; nulls are terminal values."""
        actual = self._actual
        if isinstance(actual, RowValue):
            return len(actual.fields)
        if actual.is_valid and isinstance(
            actual,
            (
                pa.StructScalar,
                pa.ListScalar,
                pa.LargeListScalar,
                pa.FixedSizeListScalar,
                pa.MapScalar,
            ),
        ):
            return len(actual)
        return 0

    @property
    def type_description(self) -> str:
        """Original type description, available even when a compact label is used."""
        return (
            "row · struct" if isinstance(self.value, RowValue) else str(self.value.type)
        )

    @property
    def metadata(self) -> str:
        """Compact type and size information, separate from rendered content."""
        if not self.is_container:
            value, metadata = scalar_leaf(self.scalar)
            if value.is_valid and isinstance(
                value,
                (
                    pa.BinaryScalar,
                    pa.LargeBinaryScalar,
                    pa.FixedSizeBinaryScalar,
                    pa.BinaryViewScalar,
                ),
            ):
                metadata += f" · {len(value.as_buffer()):,} bytes"
            return metadata
        if isinstance(self.value, RowValue):
            name = "row · struct"
        elif self._entry:
            name = "entry · struct"
        else:
            name = self.kind or "struct"
            if isinstance(self._actual, pa.LargeListScalar):
                name = "large_list"
            elif isinstance(self._actual, pa.FixedSizeListScalar):
                name = "fixed_size_list"
            if isinstance(self.value, (pa.DictionaryScalar, pa.UnionScalar)):
                name = (
                    "dictionary"
                    if isinstance(self.value, pa.DictionaryScalar)
                    else "union"
                ) + f" → {self.kind}"
        unit = {"struct": "fields", "list": "items", "map": "entries"}[
            self.kind or "struct"
        ]
        return f"{name} · {self.child_count:,} {unit}"

    def child_name(self, index: int) -> str:
        """Read only a child's identity, without fetching or formatting its value."""
        actual = self._actual
        if not 0 <= index < self.child_count:
            raise IndexError(index)
        if isinstance(actual, RowValue):
            return actual.fields[index][0]
        if isinstance(actual, pa.StructScalar):
            return (
                ("key", "value")[index]
                if self._entry
                else actual.type.field(index).name
            )
        return f"[{index}]"

    def child_height(self, index: int, width: int) -> int:
        """Measure the bounded label without rendering a child's preview."""
        if self.kind != "struct":
            return 1
        _, continuation = self._field_label(index, width)
        return 1 + continuation

    def _field_label(self, index: int, width: int) -> tuple[str, bool]:
        width = max(20, width)
        label = _quoted(self.child_name(index), width - 6)
        continuation = cell_len(label) + 18 + (index + 1 < self.child_count) > width
        return label, continuation

    def child(self, index: int) -> tuple[str, ValueNode]:
        """Read a child by position, preserving duplicate names and Map entries."""
        if not 0 <= index < self.child_count:
            raise IndexError(index)
        actual = self._actual
        if isinstance(actual, RowValue):
            name, value = actual.fields[index]
            return name, ValueNode(value)
        if isinstance(actual, pa.StructScalar):
            name = (
                ("key", "value")[index]
                if self._entry
                else actual.type.field(index).name
            )
            return name, ValueNode(actual[index])
        if isinstance(actual, pa.MapScalar) and actual.values is not None:
            return f"[{index}]", ValueNode(actual.values[index], map_entry=True)
        if isinstance(
            actual, (pa.ListScalar, pa.LargeListScalar, pa.FixedSizeListScalar)
        ):
            return f"[{index}]", ValueNode(actual[index])
        raise IndexError(index)

    def preview(
        self, width: int = 40, *, styles: Mapping[str, StyleType] | None = None
    ) -> Text:
        """Show bounded leading content with balanced quotes and container delimiters."""
        return self._preview(max(5, width), styles or _COLORS, depth=0)

    def _preview(self, width: int, styles: Mapping[str, StyleType], depth: int) -> Text:
        if self.is_container:
            left, right = self.delimiters
            if not self.child_count:
                return Text(left + right, style=styles["muted"])
            if depth >= 2 or width < 12:
                return Text(left + " … " + right, style=styles["muted"])
            result = Text(left + " ", style=styles["muted"])
            limit = 2 if self._entry else 1 if self.kind == "struct" else 3
            shown = 0
            for index in range(min(self.child_count, limit)):
                # Reserve both the closing delimiter and the remainder indicator.
                more = index + 1 < self.child_count
                available = (
                    width
                    - result.cell_len
                    - 2
                    - (3 if more else 0)
                    - (2 if index else 0)
                )
                name, child = self.child(index)
                prefix = Text()
                if self.kind == "struct":
                    if available < 10:
                        break
                    prefix.append(_quoted(name, available - 7), styles["key"])
                    prefix.append(": ", styles["muted"])
                    available -= prefix.cell_len
                if available < 5:
                    break
                if index:
                    result.append(", ", styles["muted"])
                result.append_text(prefix)
                result.append_text(child._preview(available, styles, depth + 1))
                shown += 1
            if shown < self.child_count:
                result.append((", " if shown else "") + "…", styles["muted"])
            result.append(" " + right, styles["muted"])
            return result
        value = cast(pa.Scalar, self._actual)
        if isinstance(value, pa.JsonScalar) and value.value is not None:
            value = value.value
        if not value.is_valid:
            return Text("null", style=styles["muted"])
        if isinstance(
            value, (pa.StringScalar, pa.LargeStringScalar, pa.StringViewScalar)
        ):
            buffer = value.as_buffer()
            byte_count = min(len(buffer), 4 * width)
            text = (
                buffer.slice(0, byte_count)
                .to_pybytes()
                .decode("utf-8", errors="ignore")
            )
            return Text(
                _quoted(text, width, shortened=byte_count < len(buffer)),
                style=styles["string"],
            )
        rendered = CellFormatter(inline_limit=width)(value)
        rendered.truncate(width, overflow="ellipsis")
        role = "muted"
        if pa.types.is_boolean(value.type):
            role = "boolean"
        elif (
            pa.types.is_integer(value.type)
            or pa.types.is_floating(value.type)
            or pa.types.is_decimal(value.type)
        ):
            role = "number"
        return Text(rendered.plain, style=styles[role])

    def child_prompt(
        self,
        index: int,
        *,
        width: int,
        selected: bool,
        styles: Mapping[str, StyleType] | None = None,
    ) -> Text:
        """Render one selectable child with a gutter and an optional continuation."""
        palette = styles or _COLORS
        width = max(20, width)
        name, child = self.child(index)
        result = Text("▸   " if selected else "    ", style=palette["muted"])
        comma = index + 1 < self.child_count
        if self.kind in {"list", "map"}:
            result.append(f"{index:>3}  ", palette["muted"])
        else:
            label, continuation = self._field_label(index, width)
            result.append(label, palette["key"])
            result.append(": ", palette["muted"])
            if continuation:
                result.append("\n      ")
        line_width = result.split("\n")[-1].cell_len
        result.append_text(child.preview(width - line_width - comma, styles=palette))
        if comma:
            result.append(",", palette["muted"])
        result.stylize(
            Style(
                meta={
                    "value_tooltip": f"{name}\n{child.metadata if child.is_container else child.type_description}"
                }
            )
        )
        return result
