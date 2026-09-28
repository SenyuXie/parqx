"""End-to-end smoke tests for the Parqx TUI."""

from pathlib import Path

from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from tests.helpers import wait_for


async def test_app_loads_parquet_and_navigates(small_parquet: Path) -> None:
    """Boot the app, wait for async load, drive the cursor, quit cleanly."""
    app = ParqxApp(path=small_parquet)
    async with app.run_test() as pilot:
        # Metadata is loaded in a worker before the table can request pages.
        await wait_for(lambda: not app.query_one(ArrowTable).loading, pilot)

        table = app.query_one(ArrowTable)
        assert table.row_count == 5
        assert table.column_count == 3

        await pilot.press("down", "down", "right")
        assert table.cursor_coordinate.row == 2
        assert table.cursor_coordinate.column == 1

        # Toggle bindings on ArrowTable.
        await pilot.press("h")
        assert table.show_header is False
        await pilot.press("c")
        assert table.cursor_type == "row"

    assert app.load_error is None


async def test_app_reports_load_error_for_corrupt_file(tmp_path: Path) -> None:
    """A non-parquet file surfaces `load_error` instead of crashing."""
    bogus = tmp_path / "not_a_parquet.txt"
    bogus.write_text("definitely not parquet")

    app = ParqxApp(path=bogus)
    async with app.run_test() as pilot:
        # _on_load_error sets `load_error` and then calls self.exit(), which
        # marks the worker CANCELLED — so we can't await the worker. Poll the
        # observable contract (`load_error`) instead.
        await wait_for(lambda: app.load_error is not None, pilot)

    assert app.load_error is not None
