from pathlib import Path
from threading import Event
from unittest.mock import patch

import pyarrow as pa
import pytest

from parqx.data.result_store import ResultStore
from parqx.query.engine import QueryControl, QueryLimits, QueryPreview
from parqx.query.execution import execute_query


@pytest.mark.parametrize("accepted", [True, False])
def test_store_cleanup_ownership_transfers_only_on_acceptance(
    small_parquet: Path, accepted: bool
) -> None:
    control = QueryControl()
    stores: list[ResultStore] = []
    previews: list[pa.Table] = []
    progress: list[tuple[int, bool]] = []
    errors: list[str] = []

    def on_preview(preview: QueryPreview, elapsed: float) -> None:
        assert preview.truncated
        assert elapsed >= 0
        previews.append(preview.table)
        control.load_all.set()

    def accept_store(store: ResultStore) -> bool:
        stores.append(store)
        return accepted

    try:
        execute_query(
            small_parquet,
            "SELECT i, random() AS value FROM range(7) t(i)",
            control,
            QueryLimits(preview_rows=2, batch_rows=3),
            on_preview=on_preview,
            accept_store=accept_store,
            on_progress=lambda store: progress.append(
                (store.row_count, store.finished)
            ),
            on_error=errors.append,
        )
        assert control.started.is_set()
        assert control.finished.is_set()
        assert not errors
        assert len(stores) == len(previews) == 1
        store = stores[0]
        assert store.closed is not accepted
        assert store.directory.exists() is accepted
        if accepted:
            assert progress[-1] == (7, True)
            page = store.read_window(0, 7, Event())
            assert [v.as_py() for v in page.table.column(0)] == list(range(7))
            assert page.table.column(1)[0] == previews[0].column(1)[0]
        else:
            assert not progress
            assert store.row_count == 2
    finally:
        for store in stores:
            store.close()


@pytest.mark.parametrize("before_handoff", [True, False])
def test_write_failure_cleans_unaccepted_store_and_retains_accepted_prefix(
    small_parquet: Path, before_handoff: bool
) -> None:
    control = QueryControl()
    control.load_all.set()
    stores: list[ResultStore] = []
    accepted: list[ResultStore] = []
    progress: list[int] = []
    errors: list[str] = []
    append = ResultStore.append

    def failing_append(store: ResultStore, batch: pa.RecordBatch) -> None:
        if not stores:
            stores.append(store)
        if before_handoff or store.row_count >= 2:
            raise OSError("disk full")
        append(store, batch)

    def accept_store(store: ResultStore) -> bool:
        accepted.append(store)
        return True

    try:
        with patch.object(ResultStore, "append", failing_append):
            execute_query(
                small_parquet,
                "SELECT i FROM range(7) t(i)",
                control,
                QueryLimits(preview_rows=2),
                on_preview=lambda preview, elapsed: None,
                accept_store=accept_store,
                on_progress=lambda store: progress.append(store.row_count),
                on_error=errors.append,
            )
        assert control.finished.is_set()
        assert errors == ["disk full"]
        assert len(stores) == 1
        store = stores[0]
        assert store.closed is before_handoff
        if before_handoff:
            assert not accepted
            assert not progress
            assert not store.directory.exists()
        else:
            assert accepted == [store]
            assert progress == [2]
            assert not store.finished
            page = store.read_window(0, 7, Event())
            assert [v.as_py() for v in page.table.column(0)] == [0, 1]
    finally:
        for store in stores:
            store.close()


def test_cancelled_preview_finishes_without_creating_a_store(
    small_parquet: Path,
) -> None:
    control = QueryControl()
    with patch(
        "parqx.query.execution.ResultStore",
        side_effect=AssertionError("unexpected store"),
    ):
        execute_query(
            small_parquet,
            "SELECT * FROM data",
            control,
            QueryLimits(preview_rows=1),
            on_preview=lambda preview, elapsed: control.cancel(),
            accept_store=lambda store: pytest.fail("unexpected handoff"),
            on_progress=lambda store: pytest.fail("unexpected progress"),
            on_error=lambda error: pytest.fail(error),
        )
    assert control.cancelled.is_set()
    assert control.finished.is_set()
