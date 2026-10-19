"""Current-result transitions through the mounted query workspace."""

from pathlib import Path

from textual.coordinate import Coordinate
from textual.widgets import TabbedContent

from parqx.tui.app import Parqx
from parqx.tui.widgets import ArrowTable
from tests.helpers import open_query, wait_for, wait_for_query_error


async def test_draft_edits_preserve_current_result(small_parquet: Path) -> None:
    app = Parqx([small_parquet])
    async with app.run_test(size=(120, 40)) as pilot:
        tabs = app.query_one(TabbedContent)
        await wait_for(lambda: not tabs.get_pane("source-1").loading, pilot)
        query = await open_query(app, pilot)
        assert not query.query(ArrowTable)
        sql = "SELECT name FROM smoke ORDER BY id"
        query.editor.load_text(sql)
        await pilot.press("ctrl+enter")
        await wait_for(
            lambda: bool(query.query(ArrowTable)) and not query.running, pilot
        )
        table = query.query_one(ArrowTable)
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == "alice"
        query.editor.load_text("SELECT count(*) FROM smoke")
        await pilot.pause()
        assert table.row_count == 5
        assert table.columns[0].name == "name"
        query.editor.load_text(sql)
        await pilot.pause()
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == "alice"


async def test_retry_retains_previous_success_then_replaces_it_even_with_zero_rows(
    small_parquet: Path,
) -> None:
    import gc
    import weakref
    from threading import Event
    from unittest.mock import patch

    import duckdb

    started, release = Event(), Event()
    original_sql = duckdb.DuckDBPyConnection.sql
    failing_sql = 'SELECT "missing [bold]汉字[/bold]" FROM smoke'

    def delayed_sql(
        connection: duckdb.DuckDBPyConnection, sql: str
    ) -> duckdb.DuckDBPyRelation:
        if sql == failing_sql:
            started.set()
            assert release.wait(timeout=15)
        return original_sql(connection, sql)

    app = Parqx([small_parquet])
    with (
        patch.object(duckdb.DuckDBPyConnection, "sql", delayed_sql),
        patch.object(app, "notify") as notify,
    ):
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                tabs = app.query_one(TabbedContent)
                await wait_for(lambda: not tabs.get_pane("source-1").loading, pilot)
                query = await open_query(app, pilot)
                first_sql = "SELECT name FROM smoke ORDER BY id"
                query.editor.load_text(first_sql)
                await pilot.press("ctrl+enter")
                await wait_for(
                    lambda: bool(query.query(ArrowTable)) and not query.running, pilot
                )
                table = query.query_one(ArrowTable)
                first_data = weakref.ref(table.data)
                query.editor.load_text(failing_sql)
                await pilot.press("ctrl+enter")
                await wait_for(started.is_set, pilot)
                assert query.run_button.disabled
                assert table.get_cell_at(Coordinate(0, 0)).as_py() == "alice"
                assert table.row_count == 5
                release.set()
                message = await wait_for_query_error(notify, query, pilot)
                assert "missing [bold]汉字[/bold]" in message
                query.editor.load_text(first_sql)
                await pilot.pause()
                assert table.row_count == 5
                query.editor.focus()
                query.editor.load_text("SELECT 42 AS recovered")
                await pilot.press("ctrl+enter")
                await wait_for(
                    lambda: table.columns[0].name == "recovered" and not query.running,
                    pilot,
                )
                assert table.get_cell_at(Coordinate(0, 0)).as_py() == 42

                def released() -> bool:
                    gc.collect()
                    return first_data() is None

                await wait_for(released, pilot)
                query.editor.load_text("SELECT id AS empty_id FROM smoke WHERE false")
                await pilot.press("ctrl+enter")
                await wait_for(
                    lambda: table.row_count == 0 and not query.running, pilot
                )
                assert table.columns[0].name == "empty_id"
                assert tabs.tab_count == 2
            finally:
                release.set()


async def test_failure_notifies_and_allows_retry(small_parquet: Path) -> None:
    from threading import Event
    from unittest.mock import patch

    import duckdb

    started, release = Event(), Event()
    original_sql = duckdb.DuckDBPyConnection.sql

    def delayed_sql(
        connection: duckdb.DuckDBPyConnection, sql: str
    ) -> duckdb.DuckDBPyRelation:
        started.set()
        assert release.wait(timeout=15)
        return original_sql(connection, sql)

    app = Parqx([small_parquet])
    with (
        patch.object(duckdb.DuckDBPyConnection, "sql", delayed_sql),
        patch.object(app, "notify") as notify,
    ):
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                query = await open_query(app, pilot)
                query.editor.load_text("SELECT * FROM missing_table")
                await pilot.press("ctrl+enter")
                await wait_for(started.is_set, pilot)
                assert query.run_button.disabled
                assert not query.query(ArrowTable)
                release.set()
                message = await wait_for_query_error(notify, query, pilot)
                assert "missing_table" in message
                assert not query.run_button.disabled
                assert not query.query(ArrowTable)
                query.editor.load_text("SELECT 42 AS recovered")
                await pilot.pause()
                started.clear()
                release.clear()
                query.editor.focus()
                await pilot.press("ctrl+enter")
                await wait_for(started.is_set, pilot)
                assert query.run_button.disabled
                assert not query.query(ArrowTable)
                release.set()
                await wait_for(
                    lambda: bool(query.query(ArrowTable)) and not query.running, pilot
                )
                assert not query.run_button.disabled
                assert notify.call_count == 1
                assert (
                    query.query_one(ArrowTable).get_cell_at(Coordinate(0, 0)).as_py()
                    == 42
                )
            finally:
                release.set()
