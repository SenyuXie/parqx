"""Custom application budgets reach native execution and the mounted cache."""

from pathlib import Path
from unittest.mock import patch

import duckdb
from textual.coordinate import Coordinate

from parqx.config import ParqxConfig
from parqx.data.view import TableData
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from tests.helpers import run_query, wait_for


async def test_custom_budgets_reach_browsing_and_sql(small_parquet: Path) -> None:
    config = ParqxConfig(
        memory_limit="128MB",
        threads=1,
        page_rows=2,
        page_bytes=1,
        page_cache_bytes=256,
        preview_rows=2,
        preview_bytes=1,
        query_batch_rows=1,
    )
    app = ParqxApp([small_parquet], config=config)
    with patch("parqx.data.duckdb.duckdb.connect", wraps=duckdb.connect) as connect:
        async with app.run_test() as pilot:
            await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
            table = app.query_one(ArrowTable)
            assert isinstance(table.data, TableData)
            await wait_for(lambda: table.data.peek(0, 0) is not None, pilot)
            assert table.data.cache_budget == config.page_cache_bytes
            assert table.get_cell_at(Coordinate(0, 0)).as_py() == 1
            result = await run_query(app, pilot, "SELECT * FROM smoke ORDER BY id")
            assert result.row_count == 1
            assert result.get_cell_at(Coordinate(0, 0)).as_py() == 1
            assert len(connect.call_args_list) >= 3
            for call in connect.call_args_list:
                assert call.kwargs["config"]["memory_limit"] == config.memory_limit
                assert call.kwargs["config"]["threads"] == config.threads
