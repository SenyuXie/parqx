"""Explicit file selections remain independent of the table's paging viewport."""

import gc
import weakref
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pyarrow as pa
import pytest
from textual.widgets import TabbedContent

from parqx.config import ParqxConfig
from parqx.data.parquet import ParquetSource
from parqx.data.view import DataPage, PageCache
from parqx.tui.app import Parqx
from parqx.tui.widgets.arrow_table import CursorType
from parqx.tui.widgets.table_view import TableView
from parqx.tui.widgets.value_browser import ValueBrowser
from tests.helpers import open_query, select_tab, wait_for, wait_for_source_table


@pytest.mark.parametrize("cursor_type", ["cell", "row"])
async def test_uncached_selection_survives_cursor_and_viewport_changes(
    paged_values_parquet: Path, cursor_type: CursorType
) -> None:
    block, started, release = Event(), Event(), Event()
    original_read = ParquetSource.read_window

    def delayed_read(source: ParquetSource, start: int, stop: int) -> DataPage:
        if block.is_set():
            started.set()
            assert release.wait(timeout=10)
        return original_read(source, start, stop)

    app = Parqx(
        [paged_values_parquet],
        config=ParqxConfig(
            page_rows=64, page_cache_bytes=1024, page_debounce_seconds=0
        ),
    )
    with patch.object(ParquetSource, "read_window", delayed_read):
        try:
            async with app.run_test(size=(140, 40)) as pilot:
                table = await wait_for_source_table(app, pilot)
                assert table.parent is not None
                browser = table.parent.query_one(ValueBrowser)
                block.set()
                table.focus()
                table.cursor_type = cursor_type
                table.move_cursor(row=300, column=2, animate=False)
                await pilot.press("enter")
                await wait_for(started.is_set, pilot)
                assert browser.display
                assert table.has_focus
                assert "Loading" in browser.reader.text
                assert browser.root is not None
                assert browser.root.row == 300
                await pilot.press("tab", "enter")
                assert browser.reader.has_focus
                assert "Loading" in browser.reader.text
                await pilot.press("escape")
                table.move_cursor(row=500, column=0, animate=False)
                table.cursor_type = "column"
                release.set()
                await wait_for(
                    lambda: (
                        browser.options.display
                        if cursor_type == "row"
                        else "Loading" not in browser.reader.text
                    ),
                    pilot,
                )
                assert table.has_focus
                assert browser.root is not None
                assert browser.root.row == 300
                if cursor_type == "row":
                    await pilot.press("tab", "down", "down", "enter")
                assert browser.reader.text == "row 300\n" + "read me\n" * 60
        finally:
            release.set()


@pytest.mark.parametrize("new_row", [1, 500], ids=["cached", "uncached"])
@pytest.mark.parametrize("fail_old", [False, True], ids=["late-success", "late-error"])
async def test_new_selection_supersedes_a_submitted_read(
    paged_values_parquet: Path, new_row: int, fail_old: bool
) -> None:
    block, viewport_release, old_started, old_release = (
        Event(),
        Event(),
        Event(),
        Event(),
    )
    original_read = ParquetSource.read_window
    selected_reads: list[int] = []
    reads: list[tuple[int, int]] = []

    def delayed_read(source: ParquetSource, start: int, stop: int) -> DataPage:
        reads.append((start, stop))
        if block.is_set() and stop - start > 1:
            assert viewport_release.wait(timeout=10)
        if stop - start == 1:
            selected_reads.append(start)
            if start == 300:
                old_started.set()
                assert old_release.wait(timeout=10)
                if fail_old:
                    raise OSError("obsolete selection failed")
        return original_read(source, start, stop)

    app = Parqx(
        [paged_values_parquet],
        config=ParqxConfig(page_rows=64, page_debounce_seconds=0),
    )
    with (
        patch.object(ParquetSource, "read_window", delayed_read),
        patch.object(app, "notify", wraps=app.notify) as notify,
    ):
        try:
            async with app.run_test(size=(140, 40)) as pilot:
                table = await wait_for_source_table(app, pilot)
                assert table.parent is not None
                browser = table.parent.query_one(ValueBrowser)
                block.set()
                table.focus()
                table.move_cursor(row=300, column=2, animate=False)
                await pilot.press("enter")
                assert "Loading" in browser.reader.text
                viewport_release.set()
                await wait_for(old_started.is_set, pilot)
                submitted = len(reads)
                table.move_cursor(row=new_row, column=2, animate=False)
                await pilot.press("enter")
                assert browser.root is not None
                assert browser.root.row == new_row
                assert selected_reads == [300]
                assert len(reads) == submitted
                if new_row == 1:
                    assert browser.reader.text == "row 1\n" + "read me\n" * 60
                else:
                    assert "Loading" in browser.reader.text
                old_release.set()
                await wait_for(
                    lambda: (
                        browser.reader.text == f"row {new_row}\n" + "read me\n" * 60
                    ),
                    pilot,
                )
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                assert browser.root.row == new_row
                assert table.has_focus
                assert selected_reads == ([300] if new_row == 1 else [300, 500])
                notify.assert_not_called()
        finally:
            viewport_release.set()
            old_release.set()


@pytest.mark.parametrize("completion_target", ["sidebar", "query", "hidden-query"])
async def test_loading_completion_preserves_visibility_and_focus(
    paged_values_parquet: Path, completion_target: str
) -> None:
    block, release = Event(), Event()
    original_read = ParquetSource.read_window

    def delayed_read(source: ParquetSource, start: int, stop: int) -> DataPage:
        if block.is_set():
            assert release.wait(timeout=10)
        return original_read(source, start, stop)

    app = Parqx(
        [paged_values_parquet],
        config=ParqxConfig(page_rows=64, page_debounce_seconds=0),
    )
    with patch.object(ParquetSource, "read_window", delayed_read):
        try:
            async with app.run_test(size=(140, 40)) as pilot:
                table = await wait_for_source_table(app, pilot)
                assert table.parent is not None
                browser = table.parent.query_one(ValueBrowser)
                block.set()
                table.focus()
                table.cursor_type = "row"
                table.move_cursor(row=300, animate=False)
                await pilot.press("enter", "tab")
                assert browser.reader.has_focus
                assert "Loading" in browser.reader.text
                await pilot.press("v")
                assert table.has_focus
                assert not browser.display
                await pilot.press("v", "tab", "enter")
                assert browser.reader.has_focus
                assert "Loading" in browser.reader.text
                query = None
                if completion_target != "sidebar":
                    await pilot.press(
                        "v" if completion_target == "hidden-query" else "escape"
                    )
                    query = await open_query(app, pilot)
                    query.editor.load_text("SELECT 42")
                release.set()
                await wait_for(lambda: browser.options.display, pilot)
                assert browser.display == (completion_target != "hidden-query")
                if query is not None:
                    assert query.editor.has_focus
                    assert query.editor.text == "SELECT 42"
                    await select_tab(app.query_one(TabbedContent), "source-1", pilot)
                    table.focus()
                    if completion_target == "hidden-query":
                        await pilot.press("v")
                    await pilot.press("tab")
                assert browser.options.has_focus
                assert browser.options.option_count == 5
                await pilot.press("down", "enter", "enter")
                assert browser.reader.text == "person 300"
                assert browser.reader.has_focus
        finally:
            release.set()


async def test_current_read_error_replaces_old_content_and_keeps_navigation_usable(
    paged_values_parquet: Path,
) -> None:
    block, release = Event(), Event()
    original_read = ParquetSource.read_window

    def failed_read(source: ParquetSource, start: int, stop: int) -> DataPage:
        if block.is_set():
            assert release.wait(timeout=10)
        if start == 300 and stop == 301:
            raise OSError("selected [bold]read[/bold] failed")
        return original_read(source, start, stop)

    app = Parqx(
        [paged_values_parquet],
        config=ParqxConfig(page_rows=64, page_debounce_seconds=0),
    )
    with patch.object(ParquetSource, "read_window", failed_read):
        try:
            async with app.run_test(size=(140, 40)) as pilot:
                table = await wait_for_source_table(app, pilot)
                assert table.parent is not None
                browser = table.parent.query_one(ValueBrowser)
                table.focus()
                table.move_cursor(row=0, column=2)
                await pilot.press("enter")
                assert browser.reader.text.startswith("row 0\n")
                block.set()
                table.move_cursor(row=300, column=2, animate=False)
                await pilot.press("enter", "tab")
                assert browser.reader.text == "Loading value…"
                release.set()
                await wait_for(
                    lambda: browser.reader.text.startswith("Read error"), pilot
                )
                assert "selected [bold]read[/bold] failed" in browser.reader.text
                assert browser.root is not None
                assert browser.root.row == 300
                assert browser.root.value is None
                assert browser.reader.has_focus
                await pilot.press("backspace")
                assert table.has_focus
                await pilot.press("v", "v", "tab", "escape")
                assert table.has_focus
                table.move_cursor(row=1, column=2, animate=False)
                await pilot.press("enter")
                assert browser.reader.text == "row 1\n" + "read me\n" * 60
        finally:
            release.set()


async def test_page_eviction_preserves_path_scroll_and_only_the_current_root(
    paged_values_parquet: Path,
) -> None:
    app = Parqx(
        [paged_values_parquet],
        config=ParqxConfig(
            page_rows=64, page_cache_bytes=1024, page_debounce_seconds=0
        ),
    )
    async with app.run_test(size=(140, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        assert table.parent is not None
        browser = table.parent.query_one(ValueBrowser)
        cache = table.data
        assert isinstance(cache, PageCache)
        table.focus()
        table.cursor_type = "row"
        await pilot.press("enter", "tab", "down", "down", "enter", "pagedown")
        assert browser.reader.scroll_y > 0
        position = browser.reader.scroll_y
        assert browser.root is not None
        old_value = weakref.ref(browser.root.value)
        await pilot.press("escape")
        table.move_cursor(row=300, animate=False)
        await wait_for(
            lambda: cache.peek(300, 0) is not None and cache.peek(0, 0) is None, pilot
        )
        assert browser.reader.text == "row 0\n" + "read me\n" * 60
        assert browser.reader.scroll_y == position
        await pilot.press("v", "v", "tab")
        assert browser.reader.scroll_y == position
        await pilot.press("backspace")
        assert browser.options.highlighted == 3
        await pilot.press("up", "enter", "enter")
        assert browser.reader.text == "person 0"
        await pilot.press("escape")
        table.cursor_type = "cell"
        table.move_cursor(row=300, column=0)
        await pilot.press("enter")
        assert browser.reader.text == "300"

        def old_root_released() -> bool:
            gc.collect()
            return old_value() is None

        await wait_for(old_root_released, pilot)


@pytest.mark.parametrize("invalidate", ["replace", "close"])
@pytest.mark.parametrize("fail_old", [False, True], ids=["late-success", "late-error"])
async def test_invalidated_selection_cannot_deliver_or_retain_the_old_cache(
    paged_values_parquet: Path, invalidate: str, fail_old: bool
) -> None:
    block, viewport_release, selected_started, selected_release, returned = (
        Event() for _ in range(5)
    )
    original_read = ParquetSource.read_window

    def delayed_read(source: ParquetSource, start: int, stop: int) -> DataPage:
        if block.is_set() and stop - start > 1:
            assert viewport_release.wait(timeout=10)
        if start == 300 and stop == 301:
            selected_started.set()
            try:
                assert selected_release.wait(timeout=10)
                if fail_old:
                    raise OSError("invalidated selection failed")
                return original_read(source, start, stop)
            finally:
                returned.set()
        return original_read(source, start, stop)

    app = Parqx(
        [paged_values_parquet],
        config=ParqxConfig(page_rows=64, page_debounce_seconds=0),
    )
    with (
        patch.object(ParquetSource, "read_window", delayed_read),
        patch.object(app, "notify", wraps=app.notify) as notify,
    ):
        try:
            async with app.run_test(size=(140, 40)) as pilot:
                table = await wait_for_source_table(app, pilot)
                assert table.parent is not None
                view = table.parent
                assert isinstance(view, TableView)
                browser = view.browser
                block.set()
                table.focus()
                table.move_cursor(row=300, column=2, animate=False)
                await pilot.press("enter")
                assert "Loading" in browser.reader.text
                viewport_release.set()
                await wait_for(selected_started.is_set, pilot)
                old_cache = weakref.ref(table.data)
                if invalidate == "replace":
                    view.replace_data(pa.table({"replacement": [42]}))
                    await pilot.press("enter")
                    assert browser.reader.text == "42"
                else:
                    await pilot.press("ctrl+w")
                    await wait_for(lambda: not app.query("#source-1"), pilot)
                    assert browser.root is None

                def cache_released() -> bool:
                    gc.collect()
                    return old_cache() is None

                await wait_for(cache_released, pilot)
                assert not returned.is_set()
                selected_release.set()
                await wait_for(returned.is_set, pilot)
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                await pilot.pause()
                if invalidate == "replace":
                    assert browser.reader.text == "42"
                    assert table.has_focus
                else:
                    assert not app.query("#source-1")
                    assert browser.root is None
                    assert len(app.catalog.entries) == 1
                notify.assert_not_called()
        finally:
            viewport_release.set()
            selected_release.set()
