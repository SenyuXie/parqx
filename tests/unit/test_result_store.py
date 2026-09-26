from threading import Event

import pyarrow as pa
import pytest

from parqx.data.parquet import ReadCancelledError
from parqx.data.result_store import ResultStore


def test_cross_batch_random_access_and_cleanup() -> None:
    table = pa.table({"n": range(1000)})
    store = ResultStore(table.schema)
    try:
        for batch in table.to_batches(max_chunksize=75):
            store.append(batch)
        store.finish()
        assert store.row_count == 1000
        assert store.finished
        for start, stop in [(999, 1000), (73, 80), (0, 3), (500, 503)]:
            page = store.read_window(start, stop, Event())
            assert [v.as_py() for v in page.table.column(0)] == list(range(start, stop))
    finally:
        store.close()
    assert not store.directory.exists()
    store.close()
    with pytest.raises(ReadCancelledError):
        store.read_window(0, 1, Event())


def test_store_respects_byte_budget_and_publishes_incrementally() -> None:
    table = pa.table({"text": ["x" * 100] * 50})
    store = ResultStore(table.schema, page_bytes=250)
    try:
        store.append(table.to_batches()[0])
        assert not store.finished
        page = store.read_window(0, 50, Event())
        assert page.table.num_rows == 2
        assert page.table.nbytes <= 250
        cancelled = Event()
        cancelled.set()
        with pytest.raises(ReadCancelledError):
            store.read_window(0, 5, cancelled)
    finally:
        store.close()
