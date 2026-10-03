"""File SQL references and usable multi-file dialogs in small terminals."""

from pathlib import Path
from threading import Event
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
from textual.coordinate import Coordinate
from textual.widgets import Footer, Label, TabbedContent

from parqx.data.parquet import ParquetSource
from parqx.query.engine import QuerySession
from parqx.tui.app import ParqxApp
from parqx.tui.screens.query import QueryScreen
from parqx.tui.widgets import ArrowTable
from tests.test_query_app import open_query, run_query, select_tab, wait_for
from tests.test_query_keyboard import footer_keys, footer_ready


async def test_duplicate_names_expose_exact_sql_references_and_full_paths(
    tmp_path: Path,
) -> None:
    first = tmp_path / "one" / "sales report.parquet"
    second = tmp_path / "two" / first.name
    for path in (first, second):
        path.parent.mkdir()
        pq.write_table(pa.table({"amount": [1, 2]}), path)
    app = ParqxApp([first, second])
    async with app.run_test(size=(120, 24)) as pilot:
        tabs = app.query_one(TabbedContent)
        await wait_for(lambda: len(tabs.query(ArrowTable)) == 2, pilot)
        labels: list[str] = []
        for spec in app.catalog.snapshot():
            await select_tab(tabs, spec.source_id, pilot)
            pane = tabs.get_pane(spec.source_id)
            table = pane.query_one(ArrowTable)

            def first_page_loaded(source_table: ArrowTable = table) -> bool:
                return source_table.data.peek(0, 0) is not None

            await wait_for(first_page_loaded, pilot)
            status = str(pane.query_one(Label).content)
            assert f"SQL: {spec.quoted_name}" in status
            tab = tabs.get_tab(spec.source_id)
            assert spec.quoted_name in str(tab.label)
            assert str(spec.path) in str(tab.tooltip)
            labels.append(str(tab.label))
        assert labels[0] != labels[1]
        references = [spec.quoted_name for spec in app.catalog.snapshot()]
        result = await run_query(
            app,
            pilot,
            f"SELECT sum(a.amount + b.amount) FROM {references[0]} a "
            f"JOIN {references[1]} b ON a.amount = b.amount",
        )
        assert result.get_cell_at(Coordinate(0, 0)).as_py() == 6


async def test_running_query_uses_frozen_sources_until_retried(
    small_parquet: Path,
) -> None:
    pending = small_parquet.with_name("pending.parquet")
    pending.write_bytes(small_parquet.read_bytes())
    metadata_started, metadata_release = Event(), Event()
    query_started, query_release = Event(), Event()
    original_enter = QuerySession.__enter__

    def delayed_metadata(path: Path) -> ParquetSource:
        if path == pending.resolve():
            metadata_started.set()
            assert metadata_release.wait(timeout=15)
        return ParquetSource(path)

    def delayed_enter(session: QuerySession) -> QuerySession:
        query_started.set()
        assert query_release.wait(timeout=15)
        return original_enter(session)

    app = ParqxApp([small_parquet, pending])
    with (
        patch("parqx.tui.app.ParquetSource", delayed_metadata),
        patch.object(QuerySession, "__enter__", delayed_enter),
    ):
        async with app.run_test(size=(100, 24)) as pilot:
            try:
                await wait_for(metadata_started.is_set, pilot)
                await wait_for(
                    lambda: app.catalog.get("source-1").state == "ready", pilot
                )
                query = await open_query(app, pilot)
                query.editor.load_text('SELECT count(*) FROM "pending"')
                await pilot.press("enter")
                await wait_for(query_started.is_set, pilot)
                metadata_release.set()
                await wait_for(
                    lambda: app.catalog.get("source-2").state == "ready", pilot
                )
                assert query.running
                assert query.editor.has_focus
                query_release.set()
                await wait_for(lambda: query.error is not None, pilot)
                assert "pending" in (query.error or "")
                assert "Still loading" in (query.error or "")
                assert query.editor.has_focus
                await pilot.press("enter")
                await wait_for(lambda: app.screen is not query, pilot)
                table = app.query_one("#query-1").query_one(ArrowTable)
                assert table.get_cell_at(Coordinate(0, 0)).as_py() == 5
            finally:
                metadata_release.set()
                query_release.set()


def assert_compact_controls_visible(query: QueryScreen) -> None:
    dialog = query.query_one("#query-dialog")
    assert 0 <= dialog.region.x < dialog.region.right <= 40
    assert 0 <= dialog.region.y < dialog.region.bottom <= 12
    assert query.editor.region.height >= 2
    assert dialog.region.contains_region(query.editor.region)
    footer = query.query_one(Footer)
    assert footer.region.height == 1
    assert dialog.region.contains_region(footer.region)
    keys = footer_keys(footer)
    for action in ("run_query", "close"):
        key = keys[action]
        assert key.region.width > 0
        assert key.region.height == 1
        assert footer.region.contains_region(key.region)


async def test_compact_dialog_preserves_editing_and_controls_in_all_states(
    tmp_path: Path,
) -> None:
    paths = [tmp_path / f"file-{index}.parquet" for index in range(5)]
    for path in paths:
        pq.write_table(pa.table({"n": [1]}), path)
    started, release = Event(), Event()
    original_enter = QuerySession.__enter__

    def delayed_enter(session: QuerySession) -> QuerySession:
        if session.sql == "SELECT 42 AS blocked":
            started.set()
            assert release.wait(timeout=15)
        return original_enter(session)

    app = ParqxApp(paths)
    with patch.object(QuerySession, "__enter__", delayed_enter):
        async with app.run_test(size=(40, 12)) as pilot:
            try:
                await wait_for(lambda: len(app.catalog.snapshot()) == len(paths), pilot)
                query = await open_query(app, pilot)
                await pilot.pause()
                assert_compact_controls_visible(query)
                assert query.editor.has_focus
                query.editor.load_text("SELECT missing_column")
                await pilot.press("enter")
                await wait_for(lambda: query.error is not None, pilot)
                await pilot.pause()
                assert_compact_controls_visible(query)
                assert query.editor.has_focus
                assert not query.editor.read_only
                assert query.query_one("#query-status").region.height == 1

                query.editor.load_text("SELECT 4")
                query.editor.move_cursor((0, len(query.editor.text)))
                await pilot.press("2", "shift+left")
                selection = query.editor.selection
                await pilot.press("escape")
                await wait_for(lambda: app.screen is not query, pilot)
                reopened = await open_query(app, pilot)
                assert reopened is query
                assert query.editor.text == "SELECT 42"
                assert query.editor.selection == selection
                await pilot.press("ctrl+z")
                assert query.editor.text == "SELECT 4"
                await pilot.press("ctrl+y")
                assert query.editor.text == "SELECT 42"
                query.editor.move_cursor((0, len(query.editor.text)))
                await pilot.press("shift+enter")
                assert query.editor.text == "SELECT 42\n"
                assert not query.running
                assert query.editor.has_focus

                query.editor.load_text("SELECT 42 AS blocked")
                await pilot.press("enter")
                await wait_for(started.is_set, pilot)
                footer = query.query_one(Footer)
                # A rebuilt FooterKey has its disabled class before layout
                # assigns a region, so wait for both state and geometry.
                await wait_for(
                    lambda: (
                        footer_ready(footer)
                        and footer_keys(footer)["run_query"].has_class("-disabled")
                    ),
                    pilot,
                )
                assert_compact_controls_visible(query)
                assert query.editor.has_focus
                assert query.editor.read_only
                assert not footer_keys(footer)["close"].has_class("-disabled")
                await pilot.press("enter", "x", "shift+enter")
                assert query.editor.text == "SELECT 42 AS blocked"
                assert query.running
                await pilot.press("escape")
                await wait_for(lambda: app.screen is not query, pilot)
                release.set()
                await open_query(app, pilot)
                assert query.editor.has_focus
                assert not query.editor.read_only
                assert query.editor.text == "SELECT 42 AS blocked"
                await wait_for(lambda: footer_ready(footer), pilot)
                assert_compact_controls_visible(query)
            finally:
                release.set()
