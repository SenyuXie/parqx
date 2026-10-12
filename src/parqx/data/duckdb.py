"""Worker-owned DuckDB connections, cancellation and bounded Arrow results."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from typing import cast

import duckdb
import pyarrow as pa


class QueryCancelledError(Exception):
    """The caller cancelled this DuckDB operation."""


class QueryControl:
    """Cancel one operation safely from another thread, even before it starts."""

    def __init__(self, cancelled: Event | None = None) -> None:
        """Create cancellation and worker-lifecycle signals for one operation."""
        self.cancelled = cancelled if cancelled is not None else Event()
        self.started = Event()
        self.finished = Event()
        self._lock = Lock()
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._interrupt_stop = Event()
        self._interrupt_thread: Thread | None = None

    def cancel(self) -> None:
        """Record cancellation and interrupt until the connection detaches."""
        self.cancelled.set()
        with self._lock:
            if self._connection is not None:
                self._connection.interrupt()
                if self._interrupt_thread is None:
                    thread = Thread(
                        target=self._repeat_interrupt,
                        name="parqx-duckdb-cancel",
                        daemon=True,
                    )
                    # Start under the lock so detach cannot join an unstarted
                    # thread; only retain it if starting succeeds.
                    thread.start()
                    self._interrupt_thread = thread

    def _repeat_interrupt(self) -> None:
        # DuckDB clears early interrupts when starting a query. Keep retrying
        # across native entry until the worker detaches its connection.
        while not self._interrupt_stop.wait(0.01):
            with self._lock:
                if self._connection is None:
                    return
                self._connection.interrupt()

    def check(self) -> None:
        """Stop execution or reading if cancellation was requested."""
        if self.cancelled.is_set():
            raise QueryCancelledError

    def attach(self, connection: duckdb.DuckDBPyConnection) -> None:
        """Publish the worker-owned connection for interruption."""
        with self._lock:
            self.check()
            self._connection = connection

    def detach(self) -> None:
        """Stop interrupts before the worker closes its connection."""
        with self._lock:
            self._connection = None
            self._interrupt_stop.set()
            thread = self._interrupt_thread
        # The retry thread may need the lock before it can finish.
        if thread is not None:
            thread.join()


@dataclass(frozen=True)
class QueryPreview:
    """A bounded result; truncation does not limit the query's input."""

    table: pa.Table
    """Result rows retained within the preview budgets."""
    reason: str | None = None
    """Budget that stopped reading, or None for a complete result."""

    @property
    def truncated(self) -> bool:
        """Whether a preview budget stopped reading the result."""
        return self.reason is not None


@contextmanager
def connect(
    control: QueryControl, *, memory_limit: str, threads: int
) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """Own one connection and spill directory on the calling worker thread.

    Close Arrow readers inside this context. Call control.detach first if reader
    cleanup must also be protected from cross-thread interrupts.
    """
    control.check()
    with ExitStack() as resources:
        temporary = TemporaryDirectory(prefix="parqx-duckdb-")
        resources.callback(temporary.cleanup)
        connection = duckdb.connect(
            config={
                "threads": threads,
                "memory_limit": memory_limit,
                "temp_directory": temporary.name,
                "preserve_insertion_order": True,
                "python_enable_replacements": False,
            }
        )
        resources.callback(connection.close)
        try:
            control.attach(connection)
            yield connection
        finally:
            control.detach()


def read_preview(
    reader: pa.RecordBatchReader,
    control: QueryControl,
    *,
    row_limit: int,
    byte_limit: int,
) -> QueryPreview:
    """Read bounded Arrow rows, leaving reader cleanup to the caller.

    One oversized row is admitted. Decoder and engine allocations are separate
    from the retained preview budget.
    """
    control.check()
    schema = reader.schema
    batches: list[pa.RecordBatch] = []
    rows = size = 0
    reason: str | None = None
    while True:
        control.check()
        try:
            batch = reader.read_next_batch()
        except StopIteration:
            break
        control.check()
        if not batch.num_rows:
            continue
        available = min(batch.num_rows, row_limit - rows)
        if available <= 0 or size >= byte_limit:
            reason = "row limit" if available <= 0 else "byte budget"
            break
        keep = _bounded_prefix(
            batch.slice(0, available), byte_limit - size, allow_one=rows == 0
        )
        if keep:
            # A slice could retain all the discarded rows' backing buffers.
            prefix = _compact_batch(batch.slice(0, keep), copy=keep < batch.num_rows)
            batches.append(prefix)
            rows += keep
            size += prefix.nbytes
        if keep < batch.num_rows:
            reason = "row limit" if rows >= row_limit else "byte budget"
            break
    control.check()
    return QueryPreview(pa.Table.from_batches(batches, schema=schema), reason=reason)


class _BudgetExceededError(Exception):
    """Stop measuring before copying oversized dictionary values."""


def _has_dictionary(data_type: pa.DataType) -> bool:
    return isinstance(data_type, pa.DictionaryType) or any(
        _has_dictionary(data_type.field(index).type)
        for index in range(data_type.num_fields)
    )


def _bounded_prefix(batch: pa.RecordBatch, budget: int, *, allow_one: bool) -> int:
    """Find a prefix within a byte budget, optionally admitting one oversized row."""
    dictionary_columns = [
        index for index, field in enumerate(batch.schema) if _has_dictionary(field.type)
    ]
    low, high = 0, batch.num_rows
    while low < high:
        middle = (low + high + 1) // 2
        prefix = batch.slice(0, middle)
        size = prefix.nbytes
        # Slices count the entire dictionary, including unused categories. Measure
        # only referenced values, just as the retained compact batch will do.
        for index in dictionary_columns:
            column = prefix.column(index)
            try:
                size += _compact_array(column, budget=budget).nbytes - column.nbytes
            except _BudgetExceededError:
                size = budget + 1
                break
        if size <= budget:
            low = middle
        else:
            high = middle - 1
    return max(low, 1 if allow_one and batch.num_rows else 0)


def _dictionary_payload_exceeds_budget(
    array: pa.DictionaryArray, indices: list[int | None], budget: int
) -> bool:
    data_type = array.dictionary.type
    if not (
        pa.types.is_string(data_type)
        or pa.types.is_large_string(data_type)
        or pa.types.is_binary(data_type)
        or pa.types.is_large_binary(data_type)
        or pa.types.is_fixed_size_binary(data_type)
    ):
        return False
    used: set[int] = set()
    size = 0
    for index in indices:
        if index is not None and index not in used and array.dictionary[index].is_valid:
            used.add(index)
            # A singleton may include one validity byte. Excluding it gives a
            # lower bound without copying large strings just to reject a prefix.
            size += max(0, array.dictionary.slice(index, 1).nbytes - 1)
            if size > budget:
                return True
    return False


def _compact_array(
    array: pa.Array, selection: list[int] | None = None, *, budget: int | None = None
) -> pa.Array:
    if selection is None:
        selection = list(range(len(array)))
    if isinstance(array, pa.DictionaryArray):
        indices = [
            cast(int | None, array.indices[index].as_py()) for index in selection
        ]
        if budget is not None and _dictionary_payload_exceeds_budget(
            array, indices, budget
        ):
            raise _BudgetExceededError
        # Keep category order even for an ordered dictionary. Gathering only used
        # codes avoids allocations proportional to the original dictionary.
        used = sorted({index for index in indices if index is not None})
        positions = {index: position for position, index in enumerate(used)}
        values = _compact_array(array.dictionary, used, budget=budget)
        return pa.DictionaryArray.from_arrays(
            pa.array(
                [positions[index] if index is not None else None for index in indices],
                type=array.indices.type,
            ),
            values,
            ordered=array.type.ordered,
        )
    try:
        array = array.take(pa.array(selection, type=pa.int64()))
    except pa.ArrowNotImplementedError:
        # View types lack take kernels. Arrow Scalars preserve nested types and
        # nanosecond values when rebuilding, unlike conversion to Python values.
        array = pa.array([array[index] for index in selection], type=array.type)
    if not _has_dictionary(array.type):
        return array
    if isinstance(array, pa.StructArray):
        children = [
            _compact_array(array.field(index), budget=budget)
            for index in range(array.type.num_fields)
        ]
    elif isinstance(
        array, (pa.ListArray, pa.LargeListArray, pa.FixedSizeListArray, pa.MapArray)
    ):
        children = [_compact_array(array.values, budget=budget)]
    else:
        return array
    # take normalizes parent offsets; reuse its small structural buffers while
    # replacing nested children whose dictionaries still retain unused values.
    return pa.Array.from_buffers(
        array.type,
        len(array),
        array.buffers()[: array.type.num_buffers],
        null_count=array.null_count,
        children=children,
    )


def _compact_batch(batch: pa.RecordBatch, *, copy: bool = True) -> pa.RecordBatch:
    """Copy selected rows and dictionary values without discarded backing buffers."""
    return pa.RecordBatch.from_arrays(
        [
            _compact_array(batch.column(index))
            if copy or _has_dictionary(field.type)
            else batch.column(index)
            for index, field in enumerate(batch.schema)
        ],
        schema=batch.schema,
    )
