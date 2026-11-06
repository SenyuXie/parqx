"""Public child messages delivered through mounted Textual widgets."""

import pyarrow as pa
from textual import on
from textual.app import App, ComposeResult

from parqx.tui.value_document import ContainerDocument
from parqx.tui.value_node import ValueNode
from parqx.tui.widgets.value_children import ValueChildren


async def test_highlighting_and_selection_dispatch_independently() -> None:
    children = ValueChildren(classes="value-children")
    received: list[tuple[str, ContainerDocument, int]] = []

    class ChildrenApp(App[None]):
        def compose(self) -> ComposeResult:
            yield children

        @on(ValueChildren.Highlighted)
        def on_highlighted(self, event: ValueChildren.Highlighted) -> None:
            event.stop()
            received.append(("highlighted", event.document, event.index))

        @on(ValueChildren.Selected)
        def on_selected(self, event: ValueChildren.Selected) -> None:
            event.stop()
            received.append(("selected", event.document, event.index))

    async with ChildrenApp().run_test() as pilot:
        children.show(ValueNode(pa.scalar([10, 20])), width=40, selected=0, styles={})
        children.focus()
        document = children.document
        assert document is not None

        await pilot.press("down")
        assert received == [("highlighted", document, 1)]
        received.clear()

        await pilot.press("enter")
        assert received == [("selected", document, 1)]
        received.clear()

        # The opening delimiter occupies line zero; the first child is on line one.
        assert await pilot.click(children, offset=(2, 1))
        assert received == [("highlighted", document, 0), ("selected", document, 0)]
