"""Scheduling decisions at the source tab's single read slot."""

from dataclasses import replace
from unittest.mock import Mock

import pyarrow as pa
import pytest

from parqx.data.parquet import ParquetSource
from parqx.data.view import PageCache
from parqx.tui.sources.read_schedule import ReadContext, SourceReadSchedule
from parqx.tui.values.selection import BrowseRoot


@pytest.fixture
def context() -> ReadContext:
    cache = PageCache(pa.schema([]), 600, cache_bytes=1024)
    return ReadContext(Mock(spec=ParquetSource, page_rows=8), cache, None)


@pytest.mark.parametrize("timer_first", [True, False])
def test_latest_window_waits_for_debounce_and_completion(
    context: ReadContext, timer_first: bool
) -> None:
    schedule = SourceReadSchedule()
    first = schedule.request_window(0, 2, context)
    assert schedule.window_ready(first)
    active = schedule.take_next(context)
    assert active is not None
    assert (active.start, active.stop) == (0, 8)

    obsolete = schedule.request_window(20, 24, context)
    latest = schedule.request_window(0, 2, context)
    assert not schedule.window_ready(obsolete)
    if timer_first:
        assert schedule.window_ready(latest)
    assert schedule.take_next(context) is None
    assert schedule.finish(active, context) == "discard"

    if not timer_first:
        assert schedule.take_next(context) is None
        assert schedule.window_ready(latest)
    next_read = schedule.take_next(context)
    assert next_read is not None
    assert (next_read.start, next_read.stop) == (0, 8)
    assert schedule.finish(next_read, context) == "table"
    assert schedule.take_next(context) is None


@pytest.mark.parametrize("selection", ["original", "cached", "reselected"])
def test_selection_precedes_the_viewport_and_uses_current_root_on_delivery(
    context: ReadContext, selection: str
) -> None:
    schedule = SourceReadSchedule()
    root = BrowseRoot(1, 300, 0, "id", None)
    selected = replace(context, pending_root=root)
    window = schedule.request_window(500, 504, selected)
    assert schedule.window_ready(window)
    read = schedule.take_next(selected)
    assert read is not None
    assert (read.start, read.stop) == (300, 301)

    # Scrolling supersedes the viewport even if the selected row stays the same.
    window = schedule.request_window(520, 528, context)
    current = selected
    if selection == "cached":
        current = context
    elif selection == "reselected":
        current = replace(context, pending_root=replace(root))
    delivery = schedule.finish(read, current)
    if selection == "original":
        assert delivery is root
    else:
        assert delivery == "discard"

    if selection == "reselected":
        newer = schedule.take_next(current)
        assert newer is not None
        assert schedule.finish(newer, current) is current.pending_root
    # Delivery cleared the pending root before the pane asks for another read.
    assert schedule.take_next(context) is None
    assert schedule.window_ready(window)
    viewport = schedule.take_next(context)
    assert viewport is not None
    assert viewport.start == 520


def test_replacement_invalidates_windows_but_preserves_the_submitted_slot(
    context: ReadContext,
) -> None:
    schedule = SourceReadSchedule()
    first = schedule.request_window(4, 6, context)
    assert schedule.window_ready(first)
    old_read = schedule.take_next(context)
    assert old_read is not None
    stopped_timer = schedule.request_window(8, 10, context)

    schedule.invalidate_window()
    assert not schedule.window_ready(stopped_timer)
    new = replace(context, cache=PageCache(pa.schema([]), 24, cache_bytes=128))
    latest = schedule.request_window(12, 14, new)
    assert schedule.window_ready(latest)
    assert schedule.take_next(new) is None
    assert schedule.finish(old_read, new) == "discard"
    current = schedule.take_next(new)
    assert current is not None
    assert current.start == 12
    assert schedule.finish(current, new) == "table"


@pytest.mark.parametrize("changed", ["source", "cache"])
def test_delivery_checks_current_data_even_if_window_has_not_changed(
    context: ReadContext, changed: str
) -> None:
    schedule = SourceReadSchedule()
    window = schedule.request_window(0, 4, context)
    assert schedule.window_ready(window)
    read = schedule.take_next(context)
    assert read is not None

    if changed == "source":
        current = replace(context, source=Mock(spec=ParquetSource))
    else:
        current = replace(
            context, cache=PageCache(pa.schema([]), 600, cache_bytes=1024)
        )
    assert schedule.finish(read, current) == "discard"
