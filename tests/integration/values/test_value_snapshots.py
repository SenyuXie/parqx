"""Visual regressions for value selection, focus, and binary reflow."""

from collections.abc import Callable

import pyarrow as pa
import pytest
from textual.app import App, ComposeResult
from textual.pilot import Pilot

from parqx.tui.widgets.arrow_table import ArrowTable
from parqx.tui.widgets.table_view import TableView


class ValueSnapshotApp(App[None]):
    def compose(self) -> ComposeResult:
        yield TableView(
            ArrowTable(
                pa.table(
                    {
                        "station": [{"name": "Oslo", "readings": [4, 8, 15]}],
                        "packet": [bytes(range(32, 96))],
                    }
                )
            )
        )


@pytest.mark.parametrize("focused", [False, True], ids=["table-focus", "sidebar-focus"])
def test_nested_selection(snap_compare: Callable[..., bool], focused: bool) -> None:
    keys = ["enter", "tab", "down", "enter", "down"]
    if not focused:
        keys.append("escape")
    assert snap_compare(ValueSnapshotApp(), press=keys, terminal_size=(90, 18))


def test_binary_reflow(snap_compare: Callable[..., bool]) -> None:
    async def widen(pilot: Pilot[None]) -> None:
        await pilot.press("right", "enter", "tab")
        await pilot.resize_terminal(132, 18)

    assert snap_compare(ValueSnapshotApp(), terminal_size=(90, 18), run_before=widen)
