"""Confirmation before discarding a query workspace's session-only analysis."""

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmClose(ModalScreen[bool]):
    """Name the target and default to preserving its SQL draft and result."""

    AUTO_FOCUS = "#cancel"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True)
    ]

    DEFAULT_CSS = """
    ConfirmClose {
        align: center middle;
        & > Vertical {
            width: 54;
            max-width: 95%;
            height: auto;
            padding: 1 2;
            border: solid $primary;
            background: $surface;
        }
        Static { height: auto; margin-bottom: 1; }
        Horizontal { height: 3; align-horizontal: right; }
        Button { margin-left: 1; }
    }
    """

    def __init__(self, title: str) -> None:
        """Capture the workspace label for this specific close request."""
        super().__init__()
        self._workspace_title = title

    def compose(self) -> ComposeResult:
        """Offer an explicit choice without changing the underlying workspace."""
        with Vertical():
            yield Static(f"Close {self._workspace_title}?", markup=False)
            yield Static(
                "Its SQL draft and query result will be discarded.", markup=False
            )
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Close", variant="error", id="close")

    def action_cancel(self) -> None:
        """Dismiss Escape as a cancellation."""
        self.dismiss(False)

    @on(Button.Pressed)
    def _choose(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "close")
