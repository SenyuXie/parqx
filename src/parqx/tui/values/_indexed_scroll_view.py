"""Worker lifecycle for the two virtual value readers."""

from textual import work
from textual.scroll_view import ScrollView
from textual.worker import Worker

from parqx.tui.values import document
from parqx.tui.values.document import ContainerDocument, ScalarDocument


class IndexedScrollView[Document: ContainerDocument | ScalarDocument](ScrollView):
    """Advance the visible document and distinguish navigation from layout.

    Reader actions advance the navigation revision when input arrives, even
    when its destination is still being indexed. Layout places the viewport
    without invalidating its saved reading position.
    """

    def __init__(self, *, classes: str) -> None:
        """Start with no document or index worker."""
        super().__init__(classes=classes)
        self.navigation_revision: int = 0
        self._placing_position = False
        self.document: Document | None = None
        self._index_worker: Worker[None] | None = None

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Count external scrolling, including wheel and scrollbar input."""
        super().watch_scroll_y(old_value, new_value)
        # Textual clamps the existing offset when the viewport changes size.
        if not self._placing_position and new_value != self.validate_scroll_y(
            old_value
        ):
            self.navigation_revision += 1

    def _place_position(self, y: float, *, x: float | None = None) -> None:
        """Apply the reader's own layout or navigation without counting it twice."""
        self._placing_position = True
        try:
            self.scroll_to(x=x, y=y, animate=False, force=True, immediate=True)
        finally:
            self._placing_position = False

    def _start_indexing(self) -> None:
        document = self.document
        if self.is_mounted and document is not None and not document.complete:
            self._index_worker = self._index(document)

    @work(group="value-index", exclusive=True)
    async def _index(self, current: Document) -> None:
        async for _ in document.index_document(current):
            if self.document is not current or not self.is_on_screen:
                return
            self._update_size()

    def _cancel_index(self) -> None:
        if self._index_worker is not None:
            self._index_worker.cancel()
            self._index_worker = None

    def _update_size(self) -> None:
        """Apply new document dimensions and the reader's pending position."""
        raise NotImplementedError

    def on_show(self) -> None:
        """Continue indexing the retained document."""
        self._start_indexing()

    def on_hide(self) -> None:
        """Pause this reader's indexing."""
        self._cancel_index()
