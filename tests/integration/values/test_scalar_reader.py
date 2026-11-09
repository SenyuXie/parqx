"""Complete scalar reading through production file and Query selections."""

from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
from textual.coordinate import Coordinate

from parqx.query.engine import QueryResult
from parqx.tui.app import Parqx
from parqx.tui.widgets.value_browser import ValueBrowser
from tests.helpers import open_query, wait_for, wait_for_source_table


async def test_nested_and_encoded_scalars_at_arrow_delivery(
    small_parquet: Path, encoded_scalar_table: pa.Table
) -> None:
    app = Parqx([small_parquet])
    async with app.run_test(size=(140, 40)) as pilot:
        await wait_for_source_table(app, pilot)
        query = await open_query(app, pilot)
        await query.show_result(QueryResult("Arrow fixture", encoded_scalar_table, 0))
        assert query.table is not None
        assert query.view is not None
        table, browser = query.table, query.view.browser
        reader = browser.reader
        table.cursor_type = "row"
        table.focus()
        await pilot.press("enter", "tab", "enter")
        assert reader.text == '{"literal": "[bold]中文[/bold]"}\n'
        assert reader.has_class("string")
        assert reader.has_focus
        assert "dictionary" in str(browser.metadata.content)
        assert "json" in str(browser.metadata.content)
        assert str(encoded_scalar_table.column(0).type) in str(browser.metadata.tooltip)
        await pilot.press("backspace", "down", "enter", "enter")
        assert reader.text == "1970-01-01 00:00:00.123456789Z"
        assert "timestamp[ns, tz=UTC]" in str(browser.metadata.content)
        await pilot.press("backspace", "down", "enter")
        assert reader.text == "-123456789 ns"
        await pilot.press("backspace", "backspace", "down", "enter")
        assert reader.text == "00010203-0405-0607-0809-0a0b0c0d0e0f"
        await pilot.press("backspace", "down", "enter")
        assert (
            bytes.fromhex(
                " ".join(
                    line.split("  ")[1] for line in browser.reader.text.splitlines()
                )
            )
            == b"\x00A~\xff\nZ"
        )
        assert "binary_view" in str(browser.metadata.content)
        assert "dictionary" in str(browser.metadata.content)
        assert "6 bytes" in str(browser.metadata.content)
        assert reader.has_focus
        assert reader.read_only
        assert not reader.soft_wrap


async def test_precise_scalars_and_binary_reflow(typed_scalars_parquet: Path) -> None:
    app = Parqx([typed_scalars_parquet])
    async with app.run_test(size=(120, 40)) as pilot:
        table = await wait_for_source_table(app, pilot)
        assert table.parent is not None
        browser = table.parent.query_one(ValueBrowser)
        reader = browser.reader
        table.focus()
        await pilot.press("enter")
        amount = table.get_cell_at(Coordinate(0, 0))
        assert isinstance(amount, pa.Scalar)
        assert reader.text == format(amount.as_py(), "f")
        assert reader.text == "12345678901234567890.1200"
        assert str(amount.type) in str(browser.metadata.content)
        assert reader.read_only
        assert reader.has_class("number")
        table.move_cursor(row=0, column=1)
        await pilot.press("enter")
        assert reader.text == "-0.0"
        table.move_cursor(row=0, column=2)
        await pilot.press("enter")
        recorded = table.get_cell_at(Coordinate(0, 2))
        assert isinstance(recorded, pa.Scalar)
        # DuckDB may normalize timestamp precision before delivery to the UI.
        assert reader.text == pc.cast(recorded, pa.string()).as_py()
        assert str(recorded.type) in str(browser.metadata.content)
        table.move_cursor(row=0, column=3)
        await pilot.press("enter", "tab")
        payload = table.get_cell_at(Coordinate(0, 3))
        assert isinstance(payload, pa.Scalar)
        data = payload.as_py()
        assert data == bytes(range(256)) * 4 + b"END"
        assert "1,027 bytes" in str(browser.metadata.content)
        assert reader.has_focus
        assert reader.read_only
        assert not reader.soft_wrap
        narrow_lines = browser.reader.text.splitlines()
        assert (
            bytes.fromhex(" ".join(line.split("  ")[1] for line in narrow_lines))
            == data
        )
        assert max(map(len, narrow_lines)) <= reader.scrollable_content_region.width
        await pilot.press("pagedown")
        assert reader.scroll_y > 0
        byte_offset = int(narrow_lines[int(reader.scroll_y)].split("  ")[0], 16)
        await pilot.resize_terminal(160, 40)
        await wait_for(lambda: len(reader.text.splitlines()) < len(narrow_lines), pilot)
        wide_lines = browser.reader.text.splitlines()
        bytes_per_line = int(wide_lines[1].split("  ")[0], 16)
        await wait_for(lambda: reader.scroll_y == byte_offset // bytes_per_line, pilot)
        visible_offset = int(wide_lines[int(reader.scroll_y)].split("  ")[0], 16)
        assert visible_offset <= byte_offset < visible_offset + bytes_per_line
        assert reader.has_focus
        assert (
            bytes.fromhex(" ".join(line.split("  ")[1] for line in wide_lines)) == data
        )
        assert max(map(len, wide_lines)) <= reader.scrollable_content_region.width
        reader.scroll_end(animate=False)
        await wait_for(lambda: reader.scroll_y == reader.max_scroll_y, pilot)
        assert reader.scroll_y > 0
        assert wide_lines[-1].rstrip().endswith("|")
        assert "45 4e 44" in " ".join(
            line.split("  ")[1].strip() for line in wide_lines
        )
        await pilot.resize_terminal(140, 40)
        await wait_for(lambda: len(reader.text.splitlines()) > len(wide_lines), pilot)
        await pilot.pause()
        assert reader.max_scroll_x == 0
        await pilot.press("escape")
        table.move_cursor(row=1, column=3)
        await pilot.press("enter")
        assert reader.text == "0x"
        assert "Empty binary" in str(browser.info.content)
        table.move_cursor(row=2, column=3)
        await pilot.press("enter")
        assert reader.text == "null"
        assert "Null value" in str(browser.info.content)
        table.move_cursor(row=0, column=4)
        await pilot.press("enter", "tab")
        assert reader.text == '  [bold]literal[/bold] 中文 👋\n{"keep": "text"}\t  '
        assert reader.soft_wrap
        assert reader.has_class("string")
        assert reader.scroll_y == 0
        text = reader.text
        await pilot.press("x", "delete")
        assert reader.text == text
