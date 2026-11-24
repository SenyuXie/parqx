"""Stable file identities and SQL names, independent of browsing widgets."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import pyarrow as pa

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def quote_identifier(name: str) -> str:
    """Quote a SQL identifier, escaping embedded double quotes."""
    return '"' + name.replace('"', '""') + '"'


def _reserve_table_name(stem: str, used_names: set[str]) -> str:
    """Choose a unique SQL name and reserve its ASCII-folded spelling."""
    name = stem
    suffix = 2
    while name.translate(_ASCII_LOWER) in used_names:
        name = f"{stem}_{suffix}"
        suffix += 1
    used_names.add(name.translate(_ASCII_LOWER))
    return name


@dataclass(frozen=True)
class SourceSpec:
    """Immutable identity and SQL name of an input file."""

    source_id: str
    """Stable catalog identifier, independent of loading state."""
    path: Path
    """Absolute file path used to open the source."""
    display_name: str
    """File name from the first input referring to this source."""
    table_name: str
    """Unique SQL table name before identifier quoting."""

    @property
    def quoted_name(self) -> str:
        """Return the table name ready to paste into SQL."""
        return quote_identifier(self.table_name)


@dataclass(frozen=True)
class SourceIssue:
    """A file-specific failure with enough context for user feedback."""

    source: SourceSpec
    """Source associated with this error or availability warning."""
    message: str
    """Error or availability explanation shown to the user."""

    def __str__(self) -> str:
        """Include the SQL name, file path and failure reason."""
        return f"{self.source.quoted_name} ({self.source.path}): {self.message}"


@dataclass(frozen=True)
class SourceEntry:
    """Current loading state of a source."""

    spec: SourceSpec
    """Source identity retained through loading and failure."""
    state: Literal["loading", "ready", "failed"] = "loading"
    """Loading state that determines whether the source is queryable."""
    issue: SourceIssue | None = None
    """Failure details when the source is in the failed state."""
    schema: pa.Schema | None = None
    """Loaded column metadata retained without a view's Arrow data or page cache."""


class SourceCatalog:
    """Track source availability and metadata reads on the UI thread."""

    def __init__(self, paths: Sequence[Path]) -> None:
        """Assign stable names before any asynchronous file loading starts."""
        self._entries: dict[str, SourceEntry] = {}
        self._metadata_reads: set[str] = set()
        self._has_opened_source = False
        seen_paths: set[Path] = set()
        seen_files: set[tuple[int, int]] = set()
        used_names: set[str] = set()
        for path in paths:
            error: str | None = None
            try:
                resolved = path.resolve()
            except (OSError, RuntimeError) as exc:
                # Keep a failed input visible even when canonicalization fails.
                resolved = path.absolute()
                error = str(exc)
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            try:
                stat = resolved.stat()
            except OSError:
                # Loading reports missing or unreadable files individually.
                pass
            else:
                if stat.st_ino:
                    identity = (stat.st_dev, stat.st_ino)
                    if identity in seen_files:
                        continue
                    seen_files.add(identity)

            table_name = _reserve_table_name(path.stem, used_names)
            source_id = f"source-{len(self._entries) + 1}"
            spec = SourceSpec(source_id, resolved, path.name, table_name)
            self._entries[source_id] = SourceEntry(spec)
            if error is not None:
                self.mark_failed(source_id, error)

    @property
    def entries(self) -> tuple[SourceEntry, ...]:
        """Return entries in first-input order without exposing mutable state."""
        return tuple(self._entries.values())

    def get(self, source_id: str) -> SourceEntry:
        """Look up a source, raising KeyError for an unknown identity."""
        return self._entries[source_id]

    @property
    def startup_failed(self) -> bool:
        """Whether all initial sources failed before any source became ready."""
        return not self._has_opened_source and all(
            entry.state == "failed" for entry in self._entries.values()
        )

    def request_metadata(self, source_id: str) -> SourceSpec | SourceIssue | None:
        """Request metadata for a new source tab.

        Args:
            source_id: The source to open or reopen.

        Returns:
            The source to read, its stored failure, or None when a read is
            already in progress. A ready source stays queryable during a read.
        """
        entry = self.get(source_id)
        if entry.issue is not None:
            return entry.issue
        if source_id in self._metadata_reads:
            return None
        self._metadata_reads.add(source_id)
        return entry.spec

    def cancel_metadata_read(self, source_id: str) -> None:
        """Release a cancelled read without changing the source's availability."""
        self._metadata_reads.discard(source_id)

    def cancel_all_metadata_reads(self) -> None:
        """Release all queued and running reads when the session shuts down."""
        self._metadata_reads.clear()

    def mark_ready(self, source_id: str, schema: pa.Schema | None = None) -> None:
        """Make a loaded source queryable and retain its lightweight schema."""
        self._entries[source_id] = replace(
            self.get(source_id), state="ready", issue=None, schema=schema
        )
        self._metadata_reads.discard(source_id)
        self._has_opened_source = True

    def mark_failed(self, source_id: str, message: str) -> None:
        """Record a loading failure without changing the source's identity."""
        entry = self.get(source_id)
        self._entries[source_id] = replace(
            entry, state="failed", issue=SourceIssue(entry.spec, message), schema=None
        )
        self._metadata_reads.discard(source_id)

    def snapshot(self) -> tuple[SourceSpec, ...]:
        """Freeze the sources available to one query in input order."""
        return tuple(
            entry.spec for entry in self._entries.values() if entry.state == "ready"
        )
