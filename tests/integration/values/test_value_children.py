"""Public child messages delivered through mounted Textual widgets."""

import pyarrow as pa
from textual import on
from textual.app import App, ComposeResult
from textual.widgets import Static

from parqx.tui.values.document import ContainerDocument
from parqx.tui.values.node import ValueNode
from parqx.tui.widgets.value_children import ValueChildren
from tests.helpers import wait_for


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
        assert children.highlighted == 0

        await pilot.press("up", "up")
        assert received == []
        assert await pilot.click(children, offset=(2, 1))
        assert received == [("selected", document, 0)]
        received.clear()

        await pilot.press("down")
        assert children.highlighted == 1
        assert received == [("highlighted", document, 1)]
        received.clear()

        await pilot.press("enter")
        assert received == [("selected", document, 1)]
        received.clear()

        # The opening delimiter occupies line zero; the first child is on line one.
        assert await pilot.click(children, offset=(2, 1))
        assert children.highlighted == 0
        assert received == [("highlighted", document, 0), ("selected", document, 0)]

        received.clear()
        children.highlighted = 1
        await pilot.press("enter")
        assert received == [("highlighted", document, 1), ("selected", document, 1)]


async def test_paging_targets_the_field_on_the_destination_line() -> None:
    children = ValueChildren(classes="value-children")

    class ChildrenApp(App[None]):
        CSS = "ValueChildren { height: 6; }"

        def compose(self) -> ComposeResult:
            yield children

    fields = {
        f"long_field_name_that_wraps_{i}" if i % 2 else f"f{i}": i for i in range(7)
    }
    async with ChildrenApp().run_test() as pilot:
        children.show(ValueNode(pa.scalar(fields)), width=40, selected=0, styles={})
        children.focus()
        # Field starts are 1, 2, 4, 5, 7, 8, 10. Each page moves five lines.
        await pilot.press("pagedown")
        assert children.highlighted == 3
        assert children.scroll_offset.y == 5
        await pilot.press("pageup")
        assert children.highlighted == 0
        assert children.scroll_offset.y == 0

        wrapped = pa.scalar({f"long_field_name_that_wraps_{i}": i for i in range(40)})
        children.show(ValueNode(wrapped), width=40, selected=0, styles={})
        for top in (5, 10, 15, 20):
            await pilot.press("pagedown")
            assert children.scroll_offset.y == top
        for top in (15, 10, 5):
            await pilot.press("pageup")
            assert children.scroll_offset.y == top


async def test_show_keeps_a_pending_page_when_the_same_container_rewraps() -> None:
    app = App[None]()
    async with app.run_test() as pilot:
        children = ValueChildren(classes="value-children")
        children.styles.height = 6
        await app.mount(children)
        await pilot.pause()
        node = ValueNode(pa.scalar({f"f{i}": i for i in range(256)}))
        children.show(node, width=40, selected=0, styles={})
        previous = children.document
        assert previous is not None
        last_indexed = previous.indexed_children - 1
        children.action_move(last_indexed)
        children.action_page(1)
        children.show(node, width=60, selected=last_indexed, styles={})
        document = children.document
        assert document is not None
        await wait_for(lambda: document.complete, pilot)
        assert children.highlighted == last_indexed + 5


async def test_home_reveals_the_opening_delimiter_when_already_on_first_child() -> None:
    app = App[None]()
    async with app.run_test() as pilot:
        children = ValueChildren(classes="value-children")
        children.styles.height = 3
        await app.mount(children)
        children.show(
            ValueNode(pa.scalar(list(range(10)))), width=40, selected=0, styles={}
        )
        children.focus()
        await pilot.pause()
        children.scroll_to(y=4, animate=False, immediate=True)
        assert children.scroll_offset.y == 4
        await pilot.press("home")
        assert children.scroll_offset.y == 0


async def test_hover_repaints_and_tooltip_follows_the_pointer() -> None:
    children = ValueChildren(classes="value-children")

    class ChildrenApp(App[None]):
        CSS = """
        ValueChildren {
            width: 40; height: 5; background: #000000;
            & > .option-list--option-hover { background: #123456; }
        }
        """

        def compose(self) -> ComposeResult:
            yield children
            yield Static("Outside", id="outside")

    app = ChildrenApp()
    async with app.run_test() as pilot:
        children.show(ValueNode(pa.scalar([10, 20])), width=40, selected=0, styles={})
        await pilot.pause()
        x, y = children.region.offset
        normal = app.screen.get_style_at(x + 2, y + 2).bgcolor

        assert await pilot.hover(children, offset=(10, 2))
        assert str(children.tooltip) == "[1]\nint64"
        hovered = app.screen.get_style_at(x + 2, y + 2).bgcolor
        assert hovered != normal

        # Padding keeps the same child hovered but has no tooltip metadata.
        assert await pilot.hover(children, offset=(30, 2))
        assert children.tooltip is None
        assert app.screen.get_style_at(x + 2, y + 2).bgcolor == hovered
        assert await pilot.hover(children, offset=(10, 2))
        assert str(children.tooltip) == "[1]\nint64"

        assert await pilot.hover("#outside")
        assert children.tooltip is None
        assert app.screen.get_style_at(x + 2, y + 2).bgcolor == normal
