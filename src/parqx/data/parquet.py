"""DuckDB metadata and bounded, ordered windows from one Parquet file."""

from __future__ import annotations

from contextlib import closing
from glob import escape as escape_glob
from pathlib import Path

import pyarrow as pa

from parqx.data.duckdb import QueryControl, connect, read_preview
from parqx.data.view import DataPage

_BATCH_ROWS = 256


class ParquetSource:
    """A file's metadata snapshot, with no retained connection or decoded data.

    Each window owns its DuckDB connection, so cancelling an obsolete read does
    not interrupt a newer read. Arrow retains only the bounded result for the UI.
    """

    def __init__(
        self, path: Path, *, page_rows: int = 4096, page_bytes: int = 4 * 1024 * 1024
    ) -> None:
        """Read row count and DuckDB's output schema without scanning data rows."""
        if min(page_rows, page_bytes) < 1:
            raise ValueError("Page budgets must be positive")
        self.path = path
        self.page_rows = page_rows
        self.page_bytes = page_bytes
        self._fingerprint = self._file_fingerprint()
        # DuckDB interprets glob characters even in a single, parameterized path.
        self._literal_path = escape_glob(str(path))
        with connect(QueryControl()) as connection:
            unsupported = connection.execute(
                "SELECT name, precision FROM parquet_schema(?) "
                "WHERE precision > 38 LIMIT 1",
                [self._literal_path],
            ).fetchone()
            if unsupported is not None:
                raise ValueError(
                    f"Column {unsupported[0]!r} has decimal precision "
                    f"{unsupported[1]}; DuckDB browsing supports at most 38."
                )
            metadata = connection.execute(
                "SELECT num_rows FROM parquet_file_metadata(?)", [self._literal_path]
            ).fetchone()
            if metadata is None:
                raise ValueError("The Parquet file has no metadata")
            self.row_count = int(metadata[0])
            relation = connection.read_parquet(
                self._literal_path, hive_partitioning=False
            ).limit(0)
            with closing(relation.to_arrow_reader(_BATCH_ROWS)) as reader:
                self.schema = reader.schema
        self._check_unchanged()

    def _file_fingerprint(self) -> tuple[int, int, int, int]:
        stat = self.path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def _check_unchanged(self) -> None:
        if self._file_fingerprint() != self._fingerprint:
            raise OSError("The source file changed. Reopen it to continue browsing.")

    def read_window(self, start: int, stop: int, control: QueryControl) -> DataPage:
        """Read an ordered window within the row and retained-byte budgets.

        One oversized row is allowed to ensure forward progress. DuckDB execution
        memory and Arrow decoding are separate from the retained-byte budget.
        """
        control.check()
        start = max(0, min(start, self.row_count))
        stop = min(max(start, stop), start + self.page_rows, self.row_count)
        if start == stop:
            return DataPage(start, pa.Table.from_batches([], schema=self.schema))
        self._check_unchanged()
        with connect(control) as connection:
            # Insertion order is enabled by connect(). OFFSET also works when the
            # file contains a real column named file_row_number.
            relation = connection.read_parquet(
                self._literal_path, hive_partitioning=False
            ).limit(stop - start, offset=start)
            control.check()
            with closing(relation.to_arrow_reader(_BATCH_ROWS)) as reader:
                try:
                    preview = read_preview(
                        reader,
                        control,
                        row_limit=stop - start,
                        byte_limit=self.page_bytes,
                    )
                finally:
                    control.detach()
        control.check()
        self._check_unchanged()
        return DataPage(start, preview.table)
