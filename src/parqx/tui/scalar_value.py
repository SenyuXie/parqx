"""Complete content for the supported scalar reader, separate from previews."""

from dataclasses import dataclass
from math import isinf, isnan
from typing import Literal, cast

import pyarrow as pa
import pyarrow.compute as pc


@dataclass(frozen=True)
class ScalarContent:
    """Literal reader text and the metadata that describes its completeness."""

    text: str
    metadata: str
    info: str
    role: Literal["string", "number", "boolean", "muted"]


def scalar_leaf(value: pa.Scalar) -> tuple[pa.Scalar, str]:
    """Unwrap encoded leaves without materializing their content."""
    wrappers: list[str] = []
    while (
        isinstance(value, (pa.DictionaryScalar, pa.UnionScalar, pa.JsonScalar))
        and value.is_valid
    ):
        if isinstance(value, pa.DictionaryScalar):
            wrappers.append("dictionary")
        elif isinstance(value, pa.UnionScalar):
            wrappers.append("union")
        else:
            wrappers.append("json")
        member = value.value
        if member is None:
            break
        value = member
    data_type = value.type
    metadata = str(data_type)
    if wrappers:
        metadata += " · " + " → ".join(wrappers)
    return value, metadata


def read_scalar(value: pa.Scalar) -> ScalarContent:
    """Format scalar text. ScalarDocument renders binary values by row."""
    complete = "Complete value\nRead only"
    value, metadata = scalar_leaf(value)
    data_type = value.type
    if not value.is_valid:
        return ScalarContent("null", metadata, "Null value", "muted")
    if (
        pa.types.is_string(data_type)
        or pa.types.is_large_string(data_type)
        or pa.types.is_string_view(data_type)
    ):
        text: str = value.as_py()
        metadata += f" · {len(text):,} characters"
        if not text:
            return ScalarContent('""', metadata, "Empty string\n0 characters", "string")
        return ScalarContent(text, metadata, complete, "string")
    if pa.types.is_integer(data_type):
        return ScalarContent(str(value.as_py()), metadata, complete, "number")
    if pa.types.is_decimal(data_type):
        return ScalarContent(format(value.as_py(), "f"), metadata, complete, "number")
    if pa.types.is_floating(data_type):
        number: float = value.as_py()
        if isnan(number):
            text = "NaN"
        elif isinf(number):
            text = "+Infinity" if number > 0 else "-Infinity"
        else:
            text = repr(number)
        return ScalarContent(text, metadata, complete, "number")
    if pa.types.is_boolean(data_type):
        return ScalarContent(
            "true" if value.as_py() else "false", metadata, complete, "boolean"
        )
    if (
        pa.types.is_date(data_type)
        or pa.types.is_time(data_type)
        or pa.types.is_timestamp(data_type)
    ):
        text = cast(str, pc.cast(value, pa.string()).as_py())
        return ScalarContent(text, metadata, complete, "number")
    if isinstance(value, pa.DurationScalar):
        unit = cast(pa.DurationType, data_type).unit
        return ScalarContent(f"{value.value} {unit}", metadata, complete, "number")
    if isinstance(value, pa.MonthDayNanoIntervalScalar):
        interval = value.as_py()
        text = f"{interval.months} months, {interval.days} days, {interval.nanoseconds} nanoseconds"
        return ScalarContent(text, metadata, complete, "number")
    if isinstance(value, pa.UuidScalar):
        return ScalarContent(str(value.as_py()), metadata, complete, "string")
    return ScalarContent(
        "Unsupported value type.\nThis type cannot be explored yet.",
        metadata,
        "Unsupported type",
        "muted",
    )
