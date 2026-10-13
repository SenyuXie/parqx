"""Current-result feedback through the mounted query workspace."""

from pathlib import Path

from textual.coordinate import Coordinate
from textual.widgets import TabbedContent

from parqx.tui.app import ParqxApp
from parqx.tui.widgets import ArrowTable
from tests.helpers import open_query, wait_for


async def test_current_result_identifies_its_executed_sql_after_draft_edits(
    small_parquet: Path,
) -> None:
    app = ParqxApp([small_parquet])
    async with app.run_test(size=(120, 40)) as pilot:
        tabs = app.query_one(TabbedContent)
        await wait_for(lambda: not tabs.get_pane("source-1").loading, pilot)
        query = await open_query(app, pilot)
        assert "No result yet" in str(query.status.content)
        assert not query.query(ArrowTable)
        sql = "SELECT name FROM smoke ORDER BY id"
        query.editor.load_text(sql)
        await pilot.press("ctrl+enter")
        await wait_for(
            lambda: bool(query.query(ArrowTable)) and not query.running, pilot
        )
        table = query.query_one(ArrowTable)
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == "alice"
        assert "5 rows" in str(query.status.content)
        assert "complete" in str(query.status.content)
        assert "previously executed SQL" not in str(query.status.content)
        query.editor.load_text("SELECT count(*) FROM smoke")
        await wait_for(
            lambda: "previously executed SQL" in str(query.status.content), pilot
        )
        assert table.row_count == 5
        assert table.columns[0].name == "name"
        query.editor.load_text(sql)
        await wait_for(
            lambda: "previously executed SQL" not in str(query.status.content), pilot
        )
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == "alice"


async def test_retry_retains_previous_success_then_replaces_it_even_with_zero_rows(
    small_parquet: Path,
) -> None:
    import gc
    import weakref
    from threading import Event
    from unittest.mock import patch

    import duckdb
    from textual.widgets import Button, TextArea

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

    app = ParqxApp([small_parquet])
    with patch.object(duckdb.DuckDBPyConnection, "sql", delayed_sql):
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
                assert "Running" in str(query.status.content)
                assert "previous successful result" in str(query.status.content)
                assert table.get_cell_at(Coordinate(0, 0)).as_py() == "alice"
                assert table.row_count == 5
                release.set()
                await wait_for(lambda: not query.running, pilot)
                assert "SQL error" in str(query.status.content)
                assert "previous successful result" in str(query.status.content)
                assert "missing [bold]汉字[/bold]" in str(query.status.content)
                query.editor.load_text(first_sql)
                await pilot.pause()
                assert "SQL error" in str(query.status.content)
                assert table.row_count == 5
                details = query.query_one(".result-details", Button)
                details.focus()
                await pilot.press("enter")
                await wait_for(lambda: app.screen.is_modal, pilot)
                report = app.screen.query_one(TextArea)
                assert report.read_only
                assert first_sql in report.text
                assert "missing [bold]汉字[/bold]" in report.text
                app.screen.query_one("#copy-error", Button).focus()
                await pilot.press("enter")
                assert "missing [bold]汉字[/bold]" in app.clipboard
                await pilot.press("escape")
                await wait_for(lambda: not app.screen.is_modal, pilot)
                query.editor.focus()
                query.editor.load_text("SELECT 42 AS recovered")
                await pilot.press("ctrl+enter")
                await wait_for(lambda: table.columns[0].name == "recovered", pilot)
                assert table.get_cell_at(Coordinate(0, 0)).as_py() == 42
                assert "SQL error" not in str(query.status.content)
                assert "1 rows" in str(query.status.content)

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
                assert "0 rows" in str(query.status.content)
                assert "complete" in str(query.status.content)
                assert "previous" not in str(query.status.content)
                assert tabs.tab_count == 2
            finally:
                release.set()


async def test_failure_without_success_stays_visible_until_another_attempt(
    small_parquet: Path,
) -> None:
    from threading import Event
    from unittest.mock import patch

    import duckdb
    from textual.widgets import Button, TextArea

    started, release = Event(), Event()
    original_sql = duckdb.DuckDBPyConnection.sql

    def delayed_sql(
        connection: duckdb.DuckDBPyConnection, sql: str
    ) -> duckdb.DuckDBPyRelation:
        started.set()
        assert release.wait(timeout=15)
        return original_sql(connection, sql)

    app = ParqxApp([small_parquet])
    with patch.object(duckdb.DuckDBPyConnection, "sql", delayed_sql):
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
                query = await open_query(app, pilot)
                query.editor.load_text("SELECT * FROM missing_table")
                await pilot.press("ctrl+enter")
                await wait_for(started.is_set, pilot)
                assert "Running" in str(query.status.content)
                assert "No result yet" in str(query.status.content)
                assert not query.query(ArrowTable)
                release.set()
                await wait_for(lambda: not query.running, pilot)
                assert "SQL error" in str(query.status.content)
                assert "missing_table" in str(query.status.content)
                assert not query.query(ArrowTable)
                query.editor.load_text("SELECT 42 AS recovered")
                await pilot.pause()
                assert "missing_table" in str(query.status.content)
                query.query_one(".result-details", Button).focus()
                await pilot.press("enter")
                await wait_for(lambda: app.screen.is_modal, pilot)
                assert "No successful result yet" in app.screen.query_one(TextArea).text
                assert "missing_table" in app.screen.query_one(TextArea).text
                await pilot.press("escape")
                await wait_for(lambda: not app.screen.is_modal, pilot)
                started.clear()
                release.clear()
                query.editor.focus()
                await pilot.press("ctrl+enter")
                await wait_for(started.is_set, pilot)
                assert "Running" in str(query.status.content)
                assert "SQL error" not in str(query.status.content)
                assert "missing_table" not in str(query.status.content)
                assert not query.query(ArrowTable)
                release.set()
                await wait_for(
                    lambda: bool(query.query(ArrowTable)) and not query.running, pilot
                )
                assert "SQL error" not in str(query.status.content)
                assert "complete" in str(query.status.content)
                assert (
                    query.query_one(ArrowTable).get_cell_at(Coordinate(0, 0)).as_py()
                    == 42
                )
            finally:
                release.set()


async def test_preview_and_unavailable_source_details_are_accessible(
    small_parquet: Path,
) -> None:
    import re

    from textual.widgets import Button, TextArea

    from parqx.config import ParqxConfig

    app = ParqxApp([small_parquet], config=ParqxConfig(preview_rows=2))
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(lambda: bool(app.query(ArrowTable)), pilot)
        await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
        small_parquet.unlink()
        query = await open_query(app, pilot)
        sql = "SELECT i FROM range(5) t(i)"
        query.editor.load_text(sql)
        await pilot.press("ctrl+enter")
        await wait_for(
            lambda: bool(query.query(ArrowTable)) and not query.running, pilot
        )
        table = query.query_one(ArrowTable)
        assert table.row_count == 2
        status = str(query.status.content)
        assert "2 rows" in status
        assert re.search(r"\d+\.\d{2}s", status)
        assert "preview" in status
        assert "total unknown" in status
        assert "1 source warning" in status
        query.query_one(".result-details", Button).focus()
        await pilot.press("enter")
        await wait_for(lambda: app.screen.is_modal, pilot)
        report = app.screen.query_one(TextArea)
        assert report.read_only
        assert sql in report.text
        assert "row limit" in report.text
        assert "total unknown" in report.text
        assert "Unavailable sources" in report.text
        assert str(small_parquet) in report.text
        assert '"smoke"' in report.text
        app.screen.query_one("#copy-details", Button).focus()
        await pilot.press("enter")
        assert app.clipboard == report.text
        await pilot.press("escape")
        await wait_for(lambda: not app.screen.is_modal, pilot)
        assert table.get_cell_at(Coordinate(0, 0)).as_py() == 0
        assert table.row_count == 2
