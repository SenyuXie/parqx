"""Large value journeys through production file and retained Query views."""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from parqx.tui import value_document
from parqx.tui.app import Parqx
from parqx.tui.value_document import ContainerDocument, ScalarDocument, index_document
from parqx.tui.widgets.value_browser import ValueBrowser
from tests.helpers import run_query, wait_for, wait_for_source_table


@pytest.mark.parametrize("query_result", [False, True], ids=["file", "query"])
async def test_large_list_last_child_and_long_unicode_are_readable(
    large_values_parquet: Path, query_result: bool
) -> None:
    app = Parqx([large_values_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        if query_result:
            table = await run_query(app, pilot, "SELECT * FROM large_values")
        assert table.parent is not None
        browser = table.parent.query_one(ValueBrowser)
        table.focus()
        await pilot.press("enter", "tab", "end")
        assert browser.options.highlighted == 100_000
        position = browser.options.scroll_y
        assert position > 99_000
        await pilot.press("enter")
        assert browser.reader.text == "99999"
        await pilot.press("backspace")
        assert browser.options.highlighted == 100_000
        assert browser.options.scroll_y == position
        await pilot.press("escape")
        table.move_cursor(column=1)
        await pilot.press("enter", "tab", "ctrl+end")
        reader = browser.reader
        await wait_for(
            lambda: reader.document is not None and reader.document.complete, pilot
        )
        await wait_for(lambda: reader.scroll_y == reader.max_scroll_y, pilot)
        assert "最后-END" in "".join(
            reader.render_line(y).text for y in range(reader.size.height)
        )
        assert reader.has_focus
        await pilot.press("v", "v", "tab")
        assert reader.scroll_y == reader.max_scroll_y
        await pilot.resize_terminal(120, 40)
        await wait_for(
            lambda: reader.document is not None and reader.document.complete, pilot
        )
        await pilot.press("ctrl+end")
        assert "最后-END" in "".join(
            reader.render_line(y).text for y in range(reader.size.height)
        )
        await pilot.press("escape")
        table.move_cursor(column=2)
        await pilot.press("enter", "tab", "ctrl+end")
        assert "END" in "".join(
            reader.render_line(y).text for y in range(reader.size.height)
        )
        await pilot.resize_terminal(160, 40)
        await pilot.press("ctrl+end")
        assert "END" in "".join(
            reader.render_line(y).text for y in range(reader.size.height)
        )
        assert reader.max_scroll_x == 0


async def test_large_map_and_struct_keep_positional_identity(
    small_parquet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from collections.abc import AsyncIterator

    from parqx.query.engine import QueryResult
    from tests.helpers import open_query

    count = 10_000
    fields = pa.StructArray.from_arrays(
        [pa.array([i]) for i in range(count)], names=["same" for _ in range(count)]
    )
    entries = pa.array(
        [[("same", i) for i in range(100_000)]], type=pa.map_(pa.string(), pa.int64())
    )
    app = Parqx([small_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        await wait_for_source_table(app, pilot)
        query = await open_query(app, pilot)
        await query.show_result(
            QueryResult(
                "Arrow fixture", pa.table({"fields": fields, "entries": entries}), 0
            )
        )
        assert query.table is not None
        assert query.view is not None
        table, browser = query.table, query.view.browser
        table.focus()
        await pilot.press("enter", "tab", "end")
        await wait_for(
            lambda: (
                browser.options.document is not None
                and browser.options.document.complete
            ),
            pilot,
        )
        assert browser.options.highlighted == count
        assert browser.options.scroll_y > count - 100
        position = browser.options.scroll_y
        await pilot.press("enter")
        assert browser.reader.text == str(count - 1)
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(
            document: ContainerDocument | ScalarDocument,
        ) -> AsyncIterator[None]:
            if isinstance(document, ContainerDocument):
                started.set()
                await release.wait()
            async for progress in index_document(document):
                yield progress

        with monkeypatch.context() as patch:
            patch.setattr(value_document, "index_document", delayed)
            try:
                await pilot.press("backspace")
                await wait_for(started.is_set, pilot)
                await pilot.press("v", "v", "tab")
                release.set()
                await wait_for(lambda: browser.options.scroll_y == position, pilot)
            finally:
                release.set()
        assert browser.options.highlighted == count
        await pilot.press("escape")
        table.move_cursor(column=1)
        await pilot.press("enter", "tab", "end", "enter", "down", "enter")
        assert browser.reader.text == "99999"
        assert len(browser.query(".value-crumb")) == 3
        await pilot.press("backspace")
        assert browser.options.highlighted == 2
        await pilot.press("backspace")
        assert browser.options.highlighted == 100_000
        assert browser.options.scroll_y > 99_000


@pytest.mark.parametrize("action", ["replace", "back", "hide", "close"])
async def test_delayed_formatting_cannot_overwrite_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    import asyncio
    from collections.abc import AsyncIterator

    path = tmp_path / "delayed_values.parquet"
    pq.write_table(
        pa.table(
            {"nested": [{"text": "中e\u0301👩🏽‍💻" * 160_000}], "next": ["latest"]}
        ),
        path,
    )
    started, release, cancelled, finished = (asyncio.Event() for _ in range(4))

    async def delayed(
        document: ContainerDocument | ScalarDocument,
    ) -> AsyncIterator[None]:
        if isinstance(document, ScalarDocument):
            started.set()
            # Simulate a batch already underway when its view stops owning it.
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
            document.advance()
            finished.set()
            yield
        async for progress in index_document(document):
            yield progress

    app = Parqx([path])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        assert table.parent is not None
        browser = table.parent.query_one(ValueBrowser)
        table.focus()
        await pilot.press("enter", "tab")
        monkeypatch.setattr(value_document, "index_document", delayed)
        try:
            await pilot.press("enter")
            await wait_for(started.is_set, pilot)
            assert browser.reader.has_focus
            if action == "replace":
                await pilot.press("escape")
                table.move_cursor(column=1)
                await pilot.press("enter")
            elif action == "back":
                await pilot.press("backspace")
            elif action == "hide":
                await pilot.press("v")
            else:
                await pilot.press("ctrl+w")
            await wait_for(cancelled.is_set, pilot)
            focused = app.focused
            selected = browser.root
            release.set()
            await wait_for(finished.is_set, pilot)
            await pilot.pause()
            assert app.focused is focused
            assert browser.root is selected
            if action == "replace":
                assert browser.reader.text == "latest"
                assert table.has_focus
            elif action == "back":
                assert browser.options.has_focus
                assert len(browser.query(".value-crumb")) == 1
            elif action == "hide":
                assert not browser.display
                assert table.has_focus
                await pilot.press("v", "tab", "ctrl+end")
                await wait_for(
                    lambda: (
                        browser.reader.document is not None
                        and browser.reader.document.complete
                    ),
                    pilot,
                )
                assert browser.reader.scroll_y == browser.reader.max_scroll_y
            else:
                assert browser.root is None
                assert not browser.is_attached
        finally:
            release.set()


async def test_long_wrapped_text_restores_after_native_maximize(
    large_values_parquet: Path,
) -> None:
    app = Parqx([large_values_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        assert table.parent is not None
        browser = table.parent.query_one(ValueBrowser)
        reader = browser.reader
        app.action_toggle_sources()
        table.move_cursor(column=3)
        table.focus()
        await pilot.press("enter", "tab", "ctrl+end")
        await wait_for(
            lambda: reader.document is not None and reader.document.complete, pilot
        )
        position = reader.scroll_y
        app.screen.action_maximize()
        await pilot.pause()
        await wait_for(
            lambda: reader.document is not None and reader.document.complete, pilot
        )
        assert reader.scroll_y < position
        app.screen.action_minimize()
        await pilot.pause()
        await wait_for(
            lambda: reader.document is not None and reader.document.complete, pilot
        )
        assert reader.scroll_y == position
        assert reader.has_focus
        assert "END" in "".join(
            reader.render_line(y).text for y in range(reader.size.height)
        )


async def test_hide_during_reflow_preserves_the_pending_source_position(
    large_values_parquet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from collections.abc import AsyncIterator

    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(
        document: ContainerDocument | ScalarDocument,
    ) -> AsyncIterator[None]:
        if isinstance(document, ScalarDocument):
            started.set()
            await release.wait()
        async for progress in index_document(document):
            yield progress

    app = Parqx([large_values_parquet])
    async with app.run_test(size=(160, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        assert table.parent is not None
        browser = table.parent.query_one(ValueBrowser)
        reader = browser.reader
        table.move_cursor(column=3)
        table.focus()
        await pilot.press("enter", "tab", "ctrl+end")
        await wait_for(
            lambda: reader.document is not None and reader.document.complete, pilot
        )
        assert reader.document is not None
        offset = reader.document.byte_offset(int(reader.scroll_y))
        monkeypatch.setattr(value_document, "index_document", delayed)
        try:
            await pilot.resize_terminal(120, 40)
            await wait_for(started.is_set, pilot)
            await pilot.press("v", "v", "tab")
            release.set()
            await wait_for(
                lambda: reader.document is not None and reader.document.complete, pilot
            )
            assert reader.document is not None
            assert reader.scroll_y == reader.document.line_at_byte(offset)
            assert reader.has_focus
        finally:
            release.set()
