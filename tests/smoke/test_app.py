"""Small real-file journey exercised on every supported CI platform."""

from pathlib import Path

from textual.coordinate import Coordinate
from textual.widgets import TabbedContent, TextArea

from parqx.data.view import PageCache
from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from tests.helpers import confirm_query_close, run_query, wait_for


async def test_open_browse_query_and_quit(small_parquet: Path) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test() as pilot:
        tabs = app.query_one(TabbedContent)
        await wait_for(lambda: bool(tabs.query(ArrowTable)), pilot)
        source = tabs.get_pane("source-1").query_one(ArrowTable)
        cache = source.data
        assert isinstance(cache, PageCache)
        await wait_for(lambda: cache.peek(0, 0) is not None, pilot)
        assert (source.row_count, source.column_count) == (5, 3)

        source.focus()
        await pilot.press("down", "down", "right")
        assert source.cursor_coordinate == Coordinate(2, 1)
        assert source.get_cell_at(source.cursor_coordinate).as_py() == "carol"

        sql = 'SELECT count(*) AS total FROM "smoke"'
        result = await run_query(app, pilot, sql)
        workspace = tabs.get_pane("query-1")
        assert workspace.query_one(TextArea).text == sql
        assert workspace.query_one(ArrowTable) is result
        assert not app.screen.is_modal
        assert tabs.tab_count == 2
        assert result.get_cell_at(Coordinate(0, 0)).as_py() == 5
        await pilot.press("ctrl+w")
        await confirm_query_close(app, pilot)
        await wait_for(lambda: tabs.tab_count == 1 and tabs.active == "source-1", pilot)
        await pilot.press("ctrl+q")

    assert app.return_code == 0
    assert not app.load_errors
