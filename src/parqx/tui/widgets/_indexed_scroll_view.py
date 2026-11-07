"""Worker lifecycle for the two virtual value readers."""

from textual import work
from textual.scroll_view import ScrollView
from textual.worker import Worker

from parqx.tui import value_document
from parqx.tui.value_document import ContainerDocument, ScalarDocument


class IndexedScrollView[Document: ContainerDocument | ScalarDocument](ScrollView):
    """Advance the visible reader's current document in bounded batches."""

    def __init__(self, *, classes: str) -> None:
        """Start with no document or index worker."""
        super().__init__(classes=classes)
        self.document: Document | None = None
        self._index_worker: Worker[None] | None = None

    def _start_indexing(self) -> None:
        document = self.document
        if self.is_mounted and document is not None and not document.complete:
            self._index_worker = self._index(document)

    @work(group="value-index", exclusive=True)
    async def _index(self, document: Document) -> None:
        async for _ in value_document.index_document(document):
            if self.document is not document or not self.is_on_screen:
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
