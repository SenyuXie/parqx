"""Incremental indices and bounded rendering over retained Arrow values.

The documents retain offsets, not a formatted copy of every value. Work units
bound both source bytes and rows; these are scheduling quanta, not content limits.
Only the current text page is decoded on demand, including during random access.
"""

from __future__ import annotations

import asyncio
import re
from array import array
from bisect import bisect_right
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Literal

import pyarrow as pa
from rich.cells import cell_len, split_graphemes

from parqx.tui.scalar_value import read_scalar, scalar_leaf
from parqx.tui.value_node import ValueNode

_INDEX_ROWS = 128
_DECODE_BYTES = 4096
_TOKENS = re.compile(r"[^\x00-\x1f\x7f-\x9f]+|[\x00-\x1f\x7f-\x9f]")


def _escape_controls(text: str) -> str:
    """Make terminal instructions visible without interpreting them."""
    return re.sub(
        r"[\x00-\x1f\x7f-\x9f]", lambda match: f"\\x{ord(match.group()):02x}", text
    )


@dataclass(frozen=True)
class ReadingPosition:
    """A viewport destination, including navigation awaiting an index batch."""

    scroll: float = 0
    byte_offset: int | None = None
    follow_end: bool = False
    reveal_selection: bool = False


class ContainerDocument:
    """Map screen lines to positional children without creating child options."""

    def __init__(self, node: ValueNode, *, width: int) -> None:
        """Use constant-time indices for sequences and compact offsets for fields."""
        self.node = node
        self.width = width
        self._starts = array("Q", [1])
        self.complete = node.kind != "struct" or not node.child_count

    @property
    def line_count(self) -> int:
        """Available lines, including the container delimiters."""
        if not self.node.child_count:
            return 1
        if self.node.kind != "struct":
            return self.node.child_count + 2
        return self._starts[-1] + int(self.complete)

    def advance(self) -> None:
        """Index a bounded batch of field labels, without visiting their values."""
        if self.complete:
            return
        start = len(self._starts) - 1
        for index in range(start, min(start + _INDEX_ROWS, self.node.child_count)):
            self._starts.append(
                self._starts[-1] + self.node.child_height(index, self.width)
            )
        self.complete = len(self._starts) == self.node.child_count + 1

    def child_line(self, index: int) -> int:
        """First screen line of an indexed child."""
        return self._starts[index] if self.node.kind == "struct" else index + 1

    def child_at_line(self, line: int) -> int | None:
        """Return a positional child, or None for a delimiter or unindexed line."""
        if line < 1 or line >= self.line_count - int(self.complete):
            return None
        return (
            bisect_right(self._starts, line) - 1
            if self.node.kind == "struct"
            else line - 1
        )

    @property
    def indexed_children(self) -> int:
        """Number of children currently addressable through the line index."""
        return (
            len(self._starts) - 1
            if self.node.kind == "struct"
            else self.node.child_count
        )


@dataclass(frozen=True)
class _TextRow:
    start: int
    end: int
    raw: str


class ScalarDocument:
    """Read typed leaves a viewport at a time, with incremental Unicode wrapping."""

    def __init__(self, value: pa.Scalar, *, width: int) -> None:
        """Keep native string/binary buffers; format only bounded scalar types."""
        self.width = max(1, width)
        self.value = value
        leaf, self._metadata = scalar_leaf(value)
        self.role: Literal["string", "number", "boolean", "muted"] = "muted"
        self.info = "Complete value\nRead only"
        self.bytes_per_line: int | None = None
        self._buffer: pa.Buffer
        self._offset_width = 8
        self._pages = array("Q")
        self._page_lines = array("Q")
        self._page_length_offsets = array("Q")
        self._row_lengths = bytearray()
        self._cached_page = -1
        self._cached_rows: list[_TextRow] = []
        self.line_count = 0
        self.complete = False
        self._characters = 0
        self._string = False
        if (
            leaf.is_valid
            and isinstance(
                leaf, (pa.StringScalar, pa.LargeStringScalar, pa.StringViewScalar)
            )
            and len(leaf.as_buffer())
        ):
            self._buffer = leaf.as_buffer()
            self.role = "string"
            self._string = True
        elif (
            leaf.is_valid
            and isinstance(
                leaf,
                (
                    pa.BinaryScalar,
                    pa.LargeBinaryScalar,
                    pa.FixedSizeBinaryScalar,
                    pa.BinaryViewScalar,
                ),
            )
            and len(leaf.as_buffer())
        ):
            self._buffer = leaf.as_buffer()
            self._offset_width = max(8, len(f"{len(self._buffer) - 1:x}"))
            self.bytes_per_line = max(
                1, min(16, (self.width - self._offset_width - 5) // 4)
            )
            self.line_count = (
                len(self._buffer) + self.bytes_per_line - 1
            ) // self.bytes_per_line
            self._metadata += f" · {len(self._buffer):,} bytes"
            self.complete = True
        else:
            content = read_scalar(value, width=self.width)
            self._buffer = pa.py_buffer(content.text.encode("utf-8"))
            self._metadata, self.info, self.role = (
                content.metadata,
                content.info,
                content.role,
            )
        self._rows = self._text_rows(0)

    @property
    def metadata(self) -> str:
        """Publish the exact character count only after indexing the whole text."""
        if self._string and self.complete:
            return f"{self._metadata} · {self._characters:,} characters"
        return self._metadata

    def _graphemes(self, offset: int) -> Iterator[str]:
        pending = b""
        carry = ""
        while offset < len(self._buffer) or pending or carry:
            data = self._buffer.slice(
                offset, min(_DECODE_BYTES, len(self._buffer) - offset)
            ).to_pybytes()
            offset += len(data)
            data = pending + data
            # At most three trailing bytes can be an incomplete UTF-8 codepoint.
            end = len(data)
            if offset < len(self._buffer):
                while end and data[end - 1] & 0xC0 == 0x80:
                    end -= 1
                if end:
                    end -= 1
            text = carry + data[:end].decode("utf-8")
            pending = data[end:]
            carry = ""
            # Rich attaches zero-width controls to preceding graphemes. Separate
            # logical breaks and tabs before measuring printable graphemes.
            for match in _TOKENS.finditer(text):
                token = match.group()
                if len(token) == 1 and (ord(token) < 32 or 127 <= ord(token) < 160):
                    yield token
                    continue
                spans, _ = split_graphemes(token)
                if offset < len(self._buffer) and match.end() == len(text):
                    carry = token[spans[-1][0] :]
                    if len(carry.encode("utf-8")) < _DECODE_BYTES:
                        spans = spans[:-1]
                    else:
                        # Continue oversized combining runs on another visual
                        # row instead of retaining and rescanning the whole run.
                        carry = ""
                for start, stop, _ in spans:
                    grapheme = token[start:stop]
                    if len(grapheme) < _DECODE_BYTES // 4:
                        yield grapheme
                        continue
                    data = grapheme.encode("utf-8")
                    while data:
                        end = min(_DECODE_BYTES, len(data))
                        while end < len(data) and data[end] & 0xC0 == 0x80:
                            end -= 1
                        yield data[:end].decode("utf-8")
                        data = data[end:]

    def _text_rows(self, offset: int) -> Iterator[_TextRow]:
        start = offset
        line = ""
        cells = 0
        after_cr = False
        for grapheme in self._graphemes(offset):
            if after_cr:
                if grapheme == "\n":
                    line += grapheme
                    offset += 1
                    yield _TextRow(start, offset, line)
                    start, line, cells, after_cr = offset, "", 0, False
                    continue
                yield _TextRow(start, offset, line)
                start, line, cells, after_cr = offset, "", 0, False
            if grapheme in {"\n", "\r", "\r\n"}:
                line += grapheme
                offset += len(grapheme)
                if grapheme == "\r":
                    after_cr = True
                else:
                    yield _TextRow(start, offset, line)
                    start, line, cells = offset, "", 0
                continue
            size = cell_len(_escape_controls(grapheme))
            size = 4 - cells % 4 if grapheme == "\t" else size
            byte_count = len(grapheme.encode("utf-8"))
            if (cells and cells + size > self.width) or (
                line and offset - start + byte_count > _DECODE_BYTES
            ):
                yield _TextRow(start, offset, line)
                start, line, cells = offset, "", 0
                size = 4 if grapheme == "\t" else size
            line += grapheme
            offset += byte_count
            cells += size
        yield _TextRow(start, offset, line)

    def advance(self) -> None:
        """Index one page and release decoded content from the previous page."""
        if self.complete:
            return
        rows: list[_TextRow] = []
        for _ in range(_INDEX_ROWS):
            row = next(self._rows, None)
            if row is None:
                self.complete = True
                break
            rows.append(row)
            if row.end - rows[0].start >= _DECODE_BYTES:
                break
        if rows:
            self._cached_page = len(self._pages)
            self._cached_rows = rows
            self._pages.append(rows[0].start)
            self._page_lines.append(self.line_count)
            self._page_length_offsets.append(len(self._row_lengths))
            # Exact row lengths make random access independent of UTF-8 decode
            # alignment, including pages beginning inside an oversized run.
            for row in rows:
                length = row.end - row.start
                while length >= 128:
                    self._row_lengths.append((length & 0x7F) | 0x80)
                    length >>= 7
                self._row_lengths.append(length)
            self.line_count += len(rows)
            self._characters += sum(len(row.raw) for row in rows)

    def _row(self, index: int) -> _TextRow:
        if not 0 <= index < self.line_count:
            raise IndexError(index)
        page = bisect_right(self._page_lines, index) - 1
        within = index - self._page_lines[page]
        if self._cached_page != page:
            stop = (
                self._page_lines[page + 1]
                if page + 1 < len(self._page_lines)
                else self.line_count
            )
            self._cached_rows = []
            cursor = self._page_length_offsets[page]
            start = self._pages[page]
            for _ in range(stop - self._page_lines[page]):
                length = shift = 0
                while True:
                    byte = self._row_lengths[cursor]
                    cursor += 1
                    length |= (byte & 0x7F) << shift
                    if byte < 128:
                        break
                    shift += 7
                raw = self._buffer.slice(start, length).to_pybytes().decode("utf-8")
                self._cached_rows.append(_TextRow(start, start + length, raw))
                start += length
            self._cached_page = page
        return self._cached_rows[within]

    def raw_line(self, index: int) -> str:
        """Return one wrapped text slice with its original logical terminator."""
        return self._row(index).raw

    def line(self, index: int) -> str:
        """Format one binary row or decode one indexed text row."""
        columns = self.bytes_per_line
        if columns is None:
            text = self.raw_line(index).rstrip("\r\n")
            if "\t" not in text:
                return _escape_controls(text)
            parts: list[str] = []
            cells = 0
            for index, part in enumerate(text.split("\t")):
                if index:
                    size = 4 - cells % 4
                    parts.append(" " * size)
                    cells += size
                part = _escape_controls(part)
                parts.append(part)
                cells += cell_len(part)
            return "".join(parts)
        if not 0 <= index < self.line_count:
            raise IndexError(index)
        offset = index * columns
        chunk = self._buffer.slice(
            offset, min(columns, len(self._buffer) - offset)
        ).to_pybytes()
        printable = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
        return f"{offset:0{self._offset_width}x}  {chunk.hex(' '):<{3 * columns - 1}}  |{printable:<{columns}}|"

    def byte_offset(self, line: int) -> int:
        """Anchor a viewport to the underlying bytes when its width changes."""
        if not self.line_count:
            return 0
        line = min(max(0, line), self.line_count - 1)
        return (
            line * self.bytes_per_line if self.bytes_per_line else self._row(line).start
        )

    def line_at_byte(self, offset: int) -> int:
        """Locate the indexed row containing a saved source offset."""
        if self.bytes_per_line:
            return offset // self.bytes_per_line
        if not self._pages:
            return 0
        page = max(0, bisect_right(self._pages, offset) - 1)
        stop = (
            self._page_lines[page + 1]
            if page + 1 < len(self._page_lines)
            else self.line_count
        )
        for index in range(self._page_lines[page], stop):
            row = self._row(index)
            if offset < row.end or row.end == len(self._buffer):
                return index
        return max(0, self.line_count - 1)


async def index_document(
    document: ContainerDocument | ScalarDocument,
) -> AsyncIterator[None]:
    """Yield between bounded batches so the owning UI can cancel or navigate."""
    while not document.complete:
        await asyncio.sleep(0)
        document.advance()
        yield
