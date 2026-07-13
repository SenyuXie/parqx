"""Format PyArrow scalars for display."""

from __future__ import annotations

import json
from dataclasses import dataclass
from math import ceil
from typing import cast

import pyarrow as pa
import pyarrow.compute as pc
from rich.cells import cell_len
from rich.text import Text


def _format_field_name(value: str) -> str:
    if value.isidentifier():
        return value
    return json.dumps(value, ensure_ascii=False)


def _single_line(value: str) -> str:
    return (
        value.replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


@dataclass(frozen=True, slots=True)
class CellFormatter:
    """Format Arrow scalars for single-line table cells."""

    inline_limit: int
    """Maximum preview width in terminal cells, excluding cell padding."""
    max_nested_depth: int = 2
    """Maximum number of nested container levels expanded inline."""

    def __post_init__(self) -> None:
        """Validate formatter limits."""
        if self.inline_limit < len("0x"):
            raise ValueError("inline_limit must be at least 2")
        if self.max_nested_depth < 0:
            raise ValueError("max_nested_depth must be non-negative")

    def __call__(self, scalar: pa.Scalar) -> Text:
        """Convert a cell into a Rich Text for display."""
        return self._format_scalar(scalar, depth=0, nested=False)

    def _format_scalar(self, scalar: pa.Scalar, depth: int, nested: bool) -> Text:
        """Convert an Arrow scalar into a single-line Rich Text."""
        if not scalar.is_valid:
            return Text("null", style="dim italic magenta")

        data_type = scalar.type

        if pa.types.is_boolean(data_type):
            return self._format_boolean(scalar)

        if (
            pa.types.is_integer(data_type)
            or pa.types.is_floating(data_type)
            or pa.types.is_decimal(data_type)
        ):
            return self._format_number(scalar)

        if pa.types.is_temporal(data_type):
            return self._format_temporal(scalar)

        if (
            pa.types.is_binary(data_type)
            or pa.types.is_large_binary(data_type)
            or pa.types.is_fixed_size_binary(data_type)
        ):
            return self._format_binary(scalar)

        if (
            pa.types.is_string(data_type)
            or pa.types.is_large_string(data_type)
            or pa.types.is_string_view(data_type)
        ):
            return self._format_string(scalar, nested=nested)

        if (
            pa.types.is_list(data_type)
            or pa.types.is_large_list(data_type)
            or pa.types.is_fixed_size_list(data_type)
        ):
            return self._format_list(scalar, depth=depth)

        if pa.types.is_struct(data_type):
            return self._format_struct(scalar, depth=depth)

        if pa.types.is_map(data_type):
            return self._format_map(scalar, depth=depth)

        return self._format_fallback(scalar, nested=nested)

    def _format_boolean(self, scalar: pa.Scalar) -> Text:
        value = cast(bool, scalar.as_py())
        return Text("true" if value else "false", style="green" if value else "red")

    def _format_number(self, scalar: pa.Scalar) -> Text:
        return Text(str(scalar.as_py()), style="cyan")

    def _format_temporal(self, scalar: pa.Scalar) -> Text:
        data_type = scalar.type

        if pa.types.is_duration(data_type):
            duration = cast(pa.DurationScalar, scalar)
            duration_type = cast(pa.DurationType, data_type)
            rendered = f"{duration.value}{duration_type.unit}"
        elif pa.types.is_interval(data_type):
            rendered = _single_line(str(scalar.as_py()))
        else:
            rendered = cast(str, pc.cast(scalar, pa.string()).as_py())

        return Text(rendered, style="yellow")

    def _format_binary(self, scalar: pa.Scalar) -> Text:
        prefix = "0x"
        buffer = cast(
            pa.BinaryScalar | pa.LargeBinaryScalar | pa.FixedSizeBinaryScalar, scalar
        ).as_buffer()
        preview_byte_count = min(
            len(buffer), ceil((self.inline_limit - len(prefix) + 1) / 2)
        )
        value = buffer.slice(0, preview_byte_count).to_pybytes()

        return Text(prefix + value.hex(), style="dim cyan")

    def _format_string(self, scalar: pa.Scalar, nested: bool) -> Text:
        value = cast(
            pa.StringScalar | pa.LargeStringScalar | pa.StringViewScalar, scalar
        )
        buffer = value.as_buffer()
        total_bytes = len(buffer)

        if total_bytes == 0:
            return Text('""' if nested else "")

        # ASCII usually fits in one pass. Multibyte UTF-8 may require more data
        # before the rendered text exceeds the terminal-cell width limit.
        byte_count = min(total_bytes, max(1, self.inline_limit + 1))

        while True:
            prefix = buffer.slice(0, byte_count).to_pybytes()

            # The byte slice may end in the middle of a multibyte UTF-8 character.
            # Ignore only that incomplete trailing character.
            decoded = prefix.decode("utf-8", errors="ignore")

            rendered = (
                json.dumps(decoded, ensure_ascii=False)
                if nested
                else _single_line(decoded)
            )

            if byte_count == total_bytes or cell_len(rendered) > self.inline_limit:
                return Text(rendered)

            byte_count = min(total_bytes, byte_count * 2)

    def _format_list(self, scalar: pa.Scalar, depth: int) -> Text:
        value = cast(
            pa.ListScalar | pa.LargeListScalar | pa.FixedSizeListScalar, scalar
        )

        if depth >= self.max_nested_depth:
            return Text("<list>", style="dim blue")

        result = Text("[", style="blue")

        for index in range(len(value)):
            if index:
                result.append(", ", style="blue")
                if self._overflows(result):
                    return result

            result.append_text(
                self._format_scalar(value[index], depth=depth + 1, nested=True)
            )
            if self._overflows(result):
                return result

        result.append("]", style="blue")
        return result

    def _format_struct(self, scalar: pa.Scalar, depth: int) -> Text:
        value = cast(pa.StructScalar, scalar)
        data_type = cast(pa.StructType, scalar.type)

        if depth >= self.max_nested_depth:
            return Text("<struct>", style="dim blue")

        result = Text("{", style="blue")

        for index in range(len(value)):
            if index:
                result.append(", ", style="blue")
                if self._overflows(result):
                    return result

            field_name = data_type[index].name
            result.append(_format_field_name(field_name), style="blue")
            result.append(": ", style="blue")

            if self._overflows(result):
                return result

            result.append_text(
                self._format_scalar(value[index], depth=depth + 1, nested=True)
            )
            if self._overflows(result):
                return result

        result.append("}", style="blue")
        return result

    def _format_map(self, scalar: pa.Scalar, depth: int) -> Text:
        value = cast(pa.MapScalar, scalar)
        entries = cast(pa.StructArray, value.values)

        if depth >= self.max_nested_depth:
            return Text("<map>", style="dim blue")

        keys = entries.field(0)
        values = entries.field(1)
        result = Text("{", style="blue")

        for index in range(len(entries)):
            if index:
                result.append(", ", style="blue")
                if self._overflows(result):
                    return result

            result.append_text(
                self._format_scalar(keys[index], depth=depth + 1, nested=True)
            )
            result.append(" => ", style="blue")

            if self._overflows(result):
                return result

            result.append_text(
                self._format_scalar(values[index], depth=depth + 1, nested=True)
            )
            if self._overflows(result):
                return result

        result.append("}", style="blue")
        return result

    def _format_fallback(self, scalar: pa.Scalar, nested: bool) -> Text:
        value = scalar.as_py()

        if isinstance(value, str):
            rendered = (
                json.dumps(value, ensure_ascii=False) if nested else _single_line(value)
            )
        else:
            rendered = _single_line(str(value))

        return Text(rendered)

    def _overflows(self, value: Text) -> bool:
        return cell_len(value.plain) > self.inline_limit
