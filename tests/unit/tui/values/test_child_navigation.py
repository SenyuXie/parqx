"""Highlighted children and their viewport during incremental indexing."""

import pyarrow as pa
import pytest

from parqx.tui.values.child_navigation import ChildNavigation
from parqx.tui.values.document import ContainerDocument
from parqx.tui.values.node import ValueNode


def test_page_moves_by_lines_and_keeps_wrapped_target_in_view() -> None:
    fields = {
        f"long_field_name_that_wraps_{i}" if i % 2 else f"f{i}": i for i in range(7)
    }
    document = ContainerDocument(ValueNode(pa.scalar(fields)), width=40)
    document.advance()
    navigation = ChildNavigation(document.node.child_count)
    navigation.page(1, height=6)
    assert navigation.scroll_position(document, scroll=0, height=6, max_scroll=6) == 5
    assert navigation.highlighted == 3

    navigation.page(-1, height=6)
    assert navigation.scroll_position(document, scroll=5, height=6, max_scroll=6) == 0
    assert navigation.highlighted == 0


@pytest.mark.parametrize("go_home", [True, False])
def test_pending_page_through_reflow_obeys_later_home(go_home: bool) -> None:
    node = ValueNode(pa.scalar({f"f{i}": i for i in range(512)}))
    document = ContainerDocument(node, width=60)
    document.advance()
    navigation = ChildNavigation(node.child_count)
    navigation.highlight(511)
    navigation.page(-1, height=6)
    assert navigation.scroll_position(document, scroll=0, height=6, max_scroll=123) == 0
    assert navigation.highlighted == 511

    document = ContainerDocument(node, width=40)
    document.advance()
    navigation.restore(navigation.capture(0))
    if go_home:
        navigation.first()
    scroll = 0.0
    while not document.complete:
        document.advance()
        scroll = navigation.scroll_position(
            document, scroll=scroll, height=6, max_scroll=document.line_count - 6
        )
    assert navigation.highlighted == (0 if go_home else 506)
    assert scroll == (0 if go_home else document.child_line(506) - 5)
