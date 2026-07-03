from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from parqx.data.cache import BoundedLRUCache
from parqx.data.source.parquet import ParquetSource

TEST_MAX_CACHE_BYTES = 256 * 1024 * 1024


def _row_group_cache(source: ParquetSource) -> BoundedLRUCache[int, pa.Table]:
    return cast(BoundedLRUCache[int, pa.Table], source.__dict__["_cache"])


def _is_closed(source: ParquetSource) -> bool:
    return cast(bool, source.__dict__["_closed"])


@pytest.fixture
def multi_row_group_parquet(tmp_path: Path) -> Path:
    path = tmp_path / "multi-row-group.parquet"
    table = pa.table(
        {
            "id": pa.array([0, 1, 2, 3, 4], type=pa.int64()),
            "name": ["zero", "one", "two", "three", "four"],
            "score": [0.5, 1.5, 2.5, 3.5, 4.5],
        }
    )
    pq.write_table(table, path, row_group_size=2)
    return path


def test_exposes_metadata_without_loading_row_groups(
    multi_row_group_parquet: Path,
) -> None:
    source = ParquetSource(
        multi_row_group_parquet, max_cache_bytes=TEST_MAX_CACHE_BYTES
    )
    try:
        assert source.row_count == 5
        assert source.column_count == 3
        assert [column.name for column in source.columns] == ["id", "name", "score"]
        assert len(_row_group_cache(source)) == 0
    finally:
        source.close()


def test_get_cell_at_reads_values_across_row_group_boundaries(
    multi_row_group_parquet: Path,
) -> None:
    source = ParquetSource(
        multi_row_group_parquet, max_cache_bytes=TEST_MAX_CACHE_BYTES
    )
    try:
        assert source.get_cell_at(0, 0).as_py() == 0
        assert source.get_cell_at(1, 1).as_py() == "one"
        assert source.get_cell_at(2, 1).as_py() == "two"
        assert source.get_cell_at(4, 2).as_py() == 4.5
    finally:
        source.close()


def test_get_cell_at_rejects_out_of_range_coordinates(
    multi_row_group_parquet: Path,
) -> None:
    source = ParquetSource(
        multi_row_group_parquet, max_cache_bytes=TEST_MAX_CACHE_BYTES
    )
    try:
        with pytest.raises(IndexError):
            source.get_cell_at(-1, 0)
        with pytest.raises(IndexError):
            source.get_cell_at(source.row_count, 0)
        with pytest.raises(IndexError):
            source.get_cell_at(0, -1)
        with pytest.raises(IndexError):
            source.get_cell_at(0, source.column_count)
    finally:
        source.close()


def test_get_cell_at_caches_decoded_row_groups(multi_row_group_parquet: Path) -> None:
    source = ParquetSource(
        multi_row_group_parquet, max_cache_bytes=TEST_MAX_CACHE_BYTES
    )
    cache = _row_group_cache(source)
    try:
        _ = source.get_cell_at(0, 0)
        assert len(cache) == 1

        _ = source.get_cell_at(1, 0)
        assert len(cache) == 1

        _ = source.get_cell_at(2, 0)
        assert len(cache) == 2
    finally:
        source.close()


def test_cache_limit_evicts_old_row_groups(multi_row_group_parquet: Path) -> None:
    source = ParquetSource(multi_row_group_parquet, max_cache_bytes=1)
    cache = _row_group_cache(source)
    try:
        _ = source.get_cell_at(0, 0)
        assert 0 in cache

        _ = source.get_cell_at(2, 0)

        assert 0 not in cache
        assert 1 in cache
        assert len(cache) == 1
    finally:
        source.close()


def test_close_is_idempotent_and_clears_cache(multi_row_group_parquet: Path) -> None:
    source = ParquetSource(
        multi_row_group_parquet, max_cache_bytes=TEST_MAX_CACHE_BYTES
    )
    cache = _row_group_cache(source)
    _ = source.get_cell_at(0, 0)
    assert len(cache) == 1

    source.close()

    assert _is_closed(source) is True
    assert len(cache) == 0

    source.close()
