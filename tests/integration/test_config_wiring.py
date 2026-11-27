"""Session configuration reaches native execution and mounted tables."""

from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest
from textual.coordinate import Coordinate

from parqx.config import ParqxConfig, TableDefaults
from parqx.data.parquet import ParquetSource
from parqx.data.view import DataPage, PageCache
from parqx.tui.app import Parqx
from parqx.tui.query.workspace import QueryWorkspace
from parqx.tui.table.arrow_table import ArrowTable
from tests.helpers import press_run_button, run_query, wait_for, wait_for_source_table


async def test_table_preferences_apply_once_per_table(small_parquet: Path) -> None:
    app = Parqx(
        [small_parquet],
        config=ParqxConfig(
            table=TableDefaults(
                max_column_content_width=3,
                cursor_type="row",
                zebra_stripes=True,
                show_row_index=False,
                show_header=False,
            )
        ),
    )

    def assert_initial_preferences(table: ArrowTable) -> None:
        assert table.cursor_type == "row"
        assert table.zebra_stripes
        assert not table.show_row_index
        assert not table.show_header
        assert "al…" in table.render_line(0).text

    async with app.run_test() as pilot:
        source = await wait_for_source_table(app, pilot)
        assert_initial_preferences(source)
        source.focus()
        await pilot.press("c")
        assert source.cursor_type == "column"

        result = await run_query(app, pilot, "SELECT * FROM smoke ORDER BY id")
        assert_initial_preferences(result)
        await pilot.press("c", "h", "i", "z")

        query = app.query_one(QueryWorkspace)
        query.editor.load_text("SELECT * FROM smoke WHERE id = 1")
        await press_run_button(query, pilot)
        await wait_for(
            lambda: query.table is not None and query.table.row_count == 1, pilot
        )
        rerun = query.query_one(ArrowTable)
        assert rerun.cursor_type == "column"
        assert rerun.show_header
        assert rerun.show_row_index
        assert not rerun.zebra_stripes


@pytest.mark.parametrize(
    ("page_rows", "page_bytes", "expected_rows"), [(2, 1024, 2), (5, 1, 1)]
)
async def test_custom_budgets_reach_browsing_and_sql(
    small_parquet: Path, page_rows: int, page_bytes: int, expected_rows: int
) -> None:
    config = ParqxConfig(
        memory_limit="128MB",
        threads=1,
        page_rows=page_rows,
        page_bytes=page_bytes,
        page_cache_bytes=256,
        preview_rows=2,
        preview_bytes=1,
        query_batch_rows=1,
    )
    pages: list[DataPage] = []
    original_read = ParquetSource.read_window

    def read_window(source: ParquetSource, start: int, stop: int) -> DataPage:
        page = original_read(source, start, stop)
        pages.append(page)
        return page

    app = Parqx([small_parquet], config=config)
    with (
        patch("parqx.data.duckdb.duckdb.connect", wraps=duckdb.connect) as connect,
        patch.object(ParquetSource, "read_window", read_window),
    ):
        async with app.run_test() as pilot:
            await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
            table = app.query_one(ArrowTable)
            cache = table.data
            assert isinstance(cache, PageCache)
            assert isinstance(table.data, PageCache)
            await wait_for(lambda: cache.peek(0, 0) is not None, pilot)
            assert pages[0].table.num_rows == expected_rows
            assert cache.cache_budget == config.page_cache_bytes
            assert table.get_cell_at(Coordinate(0, 0)).as_py() == 1
            result = await run_query(app, pilot, "SELECT * FROM smoke ORDER BY id")
            assert result.row_count == 1
            assert result.get_cell_at(Coordinate(0, 0)).as_py() == 1
            assert len(connect.call_args_list) >= 3
            for call in connect.call_args_list:
                assert call.kwargs["config"]["memory_limit"] == config.memory_limit
                assert call.kwargs["config"]["threads"] == config.threads
