"""Complete content for the supported scalar reader, separate from previews."""

from dataclasses import dataclass
from typing import Literal

import pyarrow as pa


@dataclass(frozen=True)
class ScalarContent:
    """Literal reader text and the metadata that describes its completeness."""

    text: str
    metadata: str
    info: str
    role: Literal["string", "number", "boolean", "muted"]


def read_scalar(value: pa.Scalar) -> ScalarContent:
    """Preserve supported values; never pass an unknown conversion off as complete."""
    data_type = value.type
    metadata = str(data_type)
    complete = "Complete value\nRead only"
    if not value.is_valid:
        return ScalarContent("null", metadata, "Null value", "muted")
    if pa.types.is_string(data_type):
        text: str = value.as_py()
        metadata += f" · {len(text):,} characters"
        if not text:
            return ScalarContent('""', metadata, "Empty string\n0 characters", "string")
        return ScalarContent(text, metadata, complete, "string")
    if pa.types.is_integer(data_type):
        return ScalarContent(str(value.as_py()), metadata, complete, "number")
    if pa.types.is_boolean(data_type):
        return ScalarContent(
            "true" if value.as_py() else "false", metadata, complete, "boolean"
        )
    return ScalarContent(
        "Unsupported value type.\nThis type cannot be explored yet.",
        metadata,
        "Unsupported type",
        "muted",
    )
