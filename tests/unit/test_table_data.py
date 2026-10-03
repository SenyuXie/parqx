import pyarrow as pa
import pytest

from parqx.data.view import DataPage, TableData


def test_cache_distinguishes_null_and_miss_and_evicts_least_recently_used() -> None:
    table = pa.table({"n": [1, None]})
    data = TableData(table.schema, 100, cache_bytes=2 * table.nbytes)
    assert data.peek(0, 0) is None
    data.add_page(DataPage(0, table))
    data.add_page(DataPage(10, table))
    scalar = data.peek(1, 0)
    assert scalar is not None
    assert not scalar.is_valid
    data.add_page(DataPage(20, table))
    assert data.peek(0, 0) is not None
    assert data.peek(10, 0) is None
    assert data.peek(20, 0) is not None
    assert data.cache_bytes == 2 * table.nbytes


def test_cache_replacement_page_limit_and_oversized_page_exception() -> None:
    table = pa.table({"n": [1]})
    data = TableData(table.schema, 1000)
    for start in range(129):
        data.add_page(DataPage(start, table))
    assert data.peek(0, 0) is None
    assert data.peek(1, 0) is not None
    assert data.cache_bytes == 128 * table.nbytes
    data.add_page(DataPage(1, table))
    assert data.cache_bytes == 128 * table.nbytes

    data = TableData(table.schema, 1000, cache_bytes=1)
    data.add_page(DataPage(0, table))
    data.add_page(DataPage(1, table))
    assert data.peek(0, 0) is None
    assert data.peek(1, 0) is not None
    assert data.cache_bytes == table.nbytes


def test_loaded_table_adapter_preserves_data_and_sample() -> None:
    table = pa.table({"n": [1, None]})
    data = TableData.from_table(table)
    assert data.sample is table
    assert data.peek(0, 0) == table.column(0)[0]
    assert data.peek(1, 0) == table.column(0)[1]
    with pytest.raises(IndexError):
        data.peek(2, 0)
