"""Small real-file journey exercised on every supported CI platform."""

from pathlib import Path

from textual.coordinate import Coordinate
from textual.widgets import TabbedContent

from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from tests.helpers import run_query, wait_for


async def test_open_browse_query_and_quit(small_parquet: Path) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test() as pilot:
        tabs = app.query_one(TabbedContent)
        await wait_for(lambda: bool(tabs.query(ArrowTable)), pilot)
        source = tabs.get_pane("source-1").query_one(ArrowTable)
        await wait_for(lambda: source.data.peek(0, 0) is not None, pilot)
        assert (source.row_count, source.column_count) == (5, 3)

        source.focus()
        await pilot.press("down", "down", "right")
        assert source.cursor_coordinate == Coordinate(2, 1)
        assert source.get_cell_at(source.cursor_coordinate).as_py() == "carol"

        result = await run_query(app, pilot, 'SELECT count(*) AS total FROM "smoke"')
        assert result.get_cell_at(Coordinate(0, 0)).as_py() == 5
        await pilot.press("ctrl+w")
        await wait_for(lambda: tabs.tab_count == 1 and tabs.active == "source-1", pilot)
        await pilot.press("ctrl+q")

    assert app.return_code == 0
    assert not app.load_errors
