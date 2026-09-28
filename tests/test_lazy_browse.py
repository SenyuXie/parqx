from pathlib import Path
from threading import Event
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
from textual.coordinate import Coordinate
from textual.widgets import TextArea

from parqx.data.parquet import ParquetSource
from parqx.data.view import DataPage
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from tests.helpers import WorkerGate, wait_for


async def test_default_browse_loads_windows_and_jumps_to_last_row(
    tmp_path: Path,
) -> None:
    path = tmp_path / "large.parquet"
    pq.write_table(pa.table({"n": range(100_000)}), path, row_group_size=10_000)
    app = ParqxApp(path)
    with patch("pyarrow.parquet.read_table", side_effect=AssertionError("full read")):
        async with app.run_test() as pilot:
            await wait_for(lambda: not app.query_one(ArrowTable).loading, pilot)
            widget = app.query_one(ArrowTable)
            assert widget.row_count == 100_000
            await wait_for(lambda: widget.data.peek(0, 0) is not None, pilot)
            assert widget.data.peek(50_000, 0) is None
            await pilot.press("ctrl+end")
            await wait_for(lambda: widget.data.peek(99_999, 0) is not None, pilot)
            assert widget.get_cell_at(Coordinate(99_999, 0)).as_py() == 99_999
            assert widget.cursor_row == 99_999
            await pilot.press("ctrl+home")
            await wait_for(lambda: widget.data.peek(0, 0) is not None, pilot)
            assert widget.get_cell_at(Coordinate(0, 0)).as_py() == 0
            assert widget.data.cache_bytes <= widget.data.cache_budget


async def test_pending_io_keeps_ui_responsive_and_cannot_replace_sql(
    small_parquet: Path,
) -> None:
    gate = WorkerGate()
    original = ParquetSource.read_window

    def slow_read(
        self: ParquetSource, start: int, stop: int, cancelled: Event
    ) -> DataPage:
        gate.pause()
        return original(self, start, stop, cancelled)

    app = ParqxApp(small_parquet)
    with gate, patch.object(ParquetSource, "read_window", slow_read):
        async with app.run_test() as pilot:
            await wait_for(gate.started.is_set, pilot)
            widget = app.query_one(ArrowTable)
            assert not widget.loading
            assert widget.data.peek(0, 0) is None
            await pilot.press("down", "right", "enter")
            assert widget.cursor_coordinate == Coordinate(1, 1)
            app.action_toggle_query()
            await pilot.pause()
            app.query_one(TextArea).load_text("SELECT 42 AS answer")
            await pilot.press("f1")
            await wait_for(lambda: not app.query_running, pilot)
            gate.release.set()
            await pilot.pause()
            assert widget.row_count == 1
            assert widget.columns[0].name == "answer"
            assert widget.get_cell_at(Coordinate(0, 0)).as_py() == 42


async def test_horizontal_navigation_in_wide_lazy_table(tmp_path: Path) -> None:
    path = tmp_path / "wide.parquet"
    pq.write_table(pa.table({f"c{i}": [f"value-{i}"] for i in range(100)}), path)
    app = ParqxApp(path)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(lambda: not app.query_one(ArrowTable).loading, pilot)
        widget = app.query_one(ArrowTable)
        await wait_for(lambda: widget.data.peek(0, 99) is not None, pilot)
        await pilot.press("end")
        assert widget.cursor_column == 99
        assert widget.get_cell_at(Coordinate(0, 99)).as_py() == "value-99"
        assert "value-99" in widget.render_line(1).text
