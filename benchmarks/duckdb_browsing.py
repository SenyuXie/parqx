"""Reproducible headless browsing/query measurements, each in a fresh process.

Run with `uv run python benchmarks/duckdb_browsing.py`. JSON is written to stdout.
Timing includes Textual's headless event loop, not a physical terminal emulator.
Files are freshly generated, so the OS file cache is generally warm.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import perf_counter, sleep
from unittest.mock import patch

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import textual
from rich.text import Text

from parqx.data.result_store import ResultStore
from parqx.query.engine import QueryControl, QueryLimits, QuerySession
from parqx.tui.app import ParqxApp
from parqx.tui.cell_formatter import CellFormatter
from parqx.tui.widgets import ArrowTable


def peak_rss_mib() -> float | None:
    """Read peak process RSS on platforms providing resource.getrusage."""
    try:
        import resource
    except ImportError:
        return None
    divisor = 1024**2 if sys.platform == "darwin" else 1024
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / divisor, 2)


async def browse(path: Path) -> dict[str, int | float | None]:
    """Measure first populated viewport and a cold jump to the last row."""
    calls = 0
    original = CellFormatter.__call__

    def counted(formatter: CellFormatter, scalar: pa.Scalar) -> Text:
        nonlocal calls
        calls += 1
        return original(formatter, scalar)

    app = ParqxApp(path)
    started = perf_counter()
    with patch.object(CellFormatter, "__call__", counted):
        async with app.run_test(size=(120, 40)) as pilot:
            async with asyncio.timeout(30):
                while not app.query(ArrowTable):
                    await pilot.pause()
                widget = app.query_one(ArrowTable)
                while widget.data.peek(0, 0) is None:
                    await pilot.pause()
                await pilot.pause()
                first_ms = (perf_counter() - started) * 1000
                before = calls
                started = perf_counter()
                widget.move_cursor(row=widget.row_count - 1)
                while widget.data.peek(widget.row_count - 1, 0) is None:
                    await pilot.pause()
                await pilot.pause()
                jump_ms = (perf_counter() - started) * 1000
                return {
                    "first_viewport_ms": round(first_ms, 2),
                    "last_row_jump_ms": round(jump_ms, 2),
                    "format_calls_on_jump": calls - before,
                    "cache_bytes": widget.data.cache_bytes,
                    "rows": widget.row_count,
                    "columns": widget.column_count,
                }


def query(path: Path, sql: str, *, full: bool = False) -> dict[str, int | float | None]:
    """Measure SQL preview and optionally materialization with bounded windows."""
    started = perf_counter()
    with QuerySession(path, sql, QueryControl()) as session:
        preview = session.preview()
        preview_ms = (perf_counter() - started) * 1000
        rows = preview.table.num_rows
        result: dict[str, int | float | None] = {
            "preview_ms": round(preview_ms, 2),
            "preview_rows": rows,
            "preview_bytes": preview.table.nbytes,
        }
        if full:
            store = ResultStore(session.schema)
            try:
                for prefix in preview.table.to_batches():
                    store.append(prefix)
                while (batch := session.read_batch()) is not None:
                    store.append(batch)
                store.finish()
                result["full_rows"] = store.row_count
                result["full_ms"] = round((perf_counter() - started) * 1000, 2)
            finally:
                store.close()
        return result


def cancel(path: Path) -> dict[str, int | float | None]:
    """Interrupt a long-running native query and wait for connection cleanup."""
    control = QueryControl()
    errors: list[str] = []

    def run() -> None:
        try:
            with QuerySession(
                path, "SELECT sum(sin(i)) FROM range(1000000000) t(i)", control
            ) as session:
                session.preview()
        except Exception as exc:  # The actual interruption exception varies by phase.
            errors.append(type(exc).__name__)

    worker = Thread(target=run)
    worker.start()
    sleep(0.2)
    started = perf_counter()
    control.cancel()
    worker.join(timeout=10)
    if worker.is_alive() or not errors:
        raise RuntimeError("Query did not stop after interruption")
    return {"cancel_and_cleanup_ms": round((perf_counter() - started) * 1000, 2)}


def main() -> None:
    """Generate datasets once, then isolate the peak RSS for each measurement."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case")
    parser.add_argument("--path", type=Path)
    args = parser.parse_args()
    if args.case:
        if args.path is None:
            parser.error("--path is required with --case")
        sql = {
            "filter": "SELECT id, value FROM data WHERE id >= 999000",
            "aggregate": "SELECT bucket, avg(value) FROM data GROUP BY bucket",
            "sort": "SELECT * FROM data ORDER BY value DESC, id",
            "full": "SELECT * FROM data WHERE id < 100000 ORDER BY id",
        }
        if args.case in sql:
            result = query(args.path, sql[args.case], full=args.case == "full")
        elif args.case == "cancel":
            result = cancel(args.path)
        else:
            result = asyncio.run(browse(args.path))
        result["peak_rss_mib"] = peak_rss_mib()
        print(json.dumps(result))
        return

    with TemporaryDirectory(prefix="parqx-benchmark-") as temporary:
        root = Path(temporary)
        narrow = root / "narrow.parquet"
        pq.write_table(
            pa.table(
                {
                    "id": range(1_000_000),
                    "bucket": [i % 100 for i in range(1_000_000)],
                    "value": [float(i % 997) for i in range(1_000_000)],
                }
            ),
            narrow,
            row_group_size=100_000,
        )
        wide = root / "wide.parquet"
        values = pa.array([f"value-{i}" for i in range(2000)])
        pq.write_table(pa.table({f"column_{i}": values for i in range(500)}), wide)
        nested = root / "nested.parquet"
        pq.write_table(
            pa.table(
                {
                    "text": ["x" * 4096] * 10_000,
                    "nested": [[{"id": i, "tags": ["a", "b"]}] for i in range(10_000)],
                }
            ),
            nested,
            row_group_size=1000,
        )
        cases = {"narrow": narrow, "wide": wide, "nested": nested}
        cases.update(
            dict.fromkeys(["filter", "aggregate", "sort", "full", "cancel"], narrow)
        )
        measurements: dict[str, object] = {}
        for case, path in cases.items():
            completed = subprocess.run(  # noqa: S603 - this script and generated paths only
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--case",
                    case,
                    "--path",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            measurements[case] = json.loads(completed.stdout)
        print(
            json.dumps(
                {
                    "environment": {
                        "platform": platform.platform(),
                        "python": platform.python_version(),
                        "duckdb": duckdb.__version__,
                        "pyarrow": pa.__version__,
                        "textual": textual.__version__,
                    },
                    "limits": {
                        "preview_rows": QueryLimits().preview_rows,
                        "duckdb_memory": QueryLimits().memory_limit,
                    },
                    "measurements": measurements,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
