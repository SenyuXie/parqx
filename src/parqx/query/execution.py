"""Worker-side query lifecycle, independent of Textual and UI request versions."""

from collections.abc import Callable
from pathlib import Path
from time import perf_counter

import duckdb
import pyarrow as pa

from parqx.data.result_store import ResultStore
from parqx.data.view import ReadCancelledError
from parqx.query.engine import (
    QueryCancelledError,
    QueryControl,
    QueryLimits,
    QueryPreview,
    QuerySession,
)

PROGRESS_INTERVAL_SECONDS = 0.1


def execute_query(
    path: Path,
    sql: str,
    control: QueryControl,
    limits: QueryLimits,
    *,
    on_preview: Callable[[QueryPreview, float], None],
    accept_store: Callable[[ResultStore], bool],
    on_progress: Callable[[ResultStore], None],
    on_error: Callable[[str], None],
) -> None:
    """Execute once, pause at the preview, then optionally materialize the rest.

    All callbacks run synchronously on this worker. A UI caller must marshal them
    to its own thread and reject stale requests there. `accept_store` returning
    True transfers cleanup ownership to the caller; until then this function
    closes the store on every exit. Accepted stores survive errors/cancellation
    so their available prefix remains browsable. The session always closes here.
    """
    control.started.set()
    started = perf_counter()
    store: ResultStore | None = None
    handed_off = False
    try:
        with QuerySession(path, sql, control, limits) as session:
            preview = session.preview()
            control.check()
            on_preview(preview, perf_counter() - started)
            if not preview.truncated:
                return
            # Pause this execution rather than rerunning a possibly expensive
            # or non-deterministic query when the user asks for all rows.
            control.load_all.wait()
            control.check()
            store = ResultStore(session.schema)
            for preview_batch in preview.table.to_batches(
                max_chunksize=limits.batch_rows
            ):
                control.check()
                store.append(preview_batch)
            del preview
            control.check()
            handed_off = accept_store(store)
            if not handed_off:
                return
            last_update = perf_counter()
            while (batch := session.read_batch()) is not None:
                store.append(batch)
                if perf_counter() - last_update >= PROGRESS_INTERVAL_SECONDS:
                    on_progress(store)
                    last_update = perf_counter()
            store.finish()
            on_progress(store)
    except (duckdb.Error, pa.ArrowException, OSError, ValueError, MemoryError) as exc:
        if not control.cancelled.is_set():
            if handed_off and store is not None:
                on_progress(store)
            on_error(str(exc))
        return
    except (QueryCancelledError, ReadCancelledError):
        return
    finally:
        try:
            if store is not None and not handed_off:
                store.close()
        finally:
            control.finished.set()
