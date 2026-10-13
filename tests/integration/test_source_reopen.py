"""Recover original file views through the mounted application's sources sidebar."""

import gc
import weakref
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from textual.coordinate import Coordinate
from textual.widgets import TabbedContent

from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from parqx.tui.widgets.sources_tree import SourcesTree
from tests.helpers import run_query, select_tab, wait_for


async def test_sidebar_activates_and_reopens_one_default_view_per_source(
    tmp_path: Path,
) -> None:
    first = tmp_path / "one" / "sales.parquet"
    second = tmp_path / "two" / first.name
    for path in (first, second):
        path.parent.mkdir()
    pq.write_table(pa.table({"amount": [10, 20, 30]}), first)
    pq.write_table(pa.table({"amount": [40]}), second)
    app = ParqxApp([first, second, first])
    async with app.run_test(size=(120, 40)) as pilot:
        tabs = app.query_one(TabbedContent)
        await wait_for(lambda: len(tabs.query(ArrowTable)) == 2, pilot)
        table = tabs.get_pane("source-1").query_one(ArrowTable)
        table.focus()
        await pilot.press("h", "i", "z", "c", "down")
        await select_tab(tabs, "source-2", pilot)
        app.action_toggle_sources()
        sources = app.query_one(SourcesTree)
        sources.focus()
        await pilot.press("home", "enter")
        await wait_for(lambda: tabs.active == "source-1", pilot)
        assert tabs.tab_count == 2
        assert tabs.get_pane("source-1").query_one(ArrowTable) is table
        assert not table.show_header
        assert table.cursor_coordinate == Coordinate(1, 0)
        cache = weakref.ref(table.data)
        await pilot.press("ctrl+w")
        await wait_for(lambda: not tabs.query("#source-1"), pilot)

        def released() -> bool:
            gc.collect()
            return cache() is None

        await wait_for(released, pilot)
        assert "amount: int64" in [
            str(child.label) for child in sources.root.children[0].children
        ]
        sources.focus()
        await pilot.press("home", "enter", "enter")
        await wait_for(
            lambda: (
                tabs.active == "source-1" and bool(tabs.query("#source-1 ArrowTable"))
            ),
            pilot,
        )
        reopened = tabs.get_pane("source-1").query_one(ArrowTable)
        await wait_for(
            lambda: reopened.get_cell_at(Coordinate(0, 0)) is not None, pilot
        )
        assert reopened.get_cell_at(Coordinate(0, 0)).as_py() == 10
        assert reopened.show_header
        assert reopened.show_row_index
        assert not reopened.zebra_stripes
        assert reopened.cursor_type == "cell"
        assert reopened.cursor_coordinate == Coordinate(0, 0)
        assert tabs.tab_count == 2
        assert len(app.catalog.entries) == 2
        assert [entry.spec.table_name for entry in app.catalog.entries] == [
            "sales",
            "sales_2",
        ]
        assert '"sales"' in str(tabs.get_tab("source-1").label)
        assert str(first.resolve()) in str(tabs.get_tab("source-1").tooltip)
        sources.focus()
        await pilot.press("home", "right", "down", "down", "down", "enter")
        assert tabs.active == "source-1"
        assert tabs.tab_count == 2
        result = await run_query(app, pilot, 'SELECT sum(amount) FROM "sales"')
        assert result.get_cell_at(Coordinate(0, 0)).as_py() == 60
