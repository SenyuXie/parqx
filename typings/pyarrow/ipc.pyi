from types import TracebackType
from typing import BinaryIO, Self

from . import RecordBatch, Schema

class RecordBatchFileWriter:
    def write_batch(self, batch: RecordBatch) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

class RecordBatchFileReader:
    def get_batch(self, index: int) -> RecordBatch: ...

def new_file(sink: BinaryIO, schema: Schema) -> RecordBatchFileWriter: ...
def open_file(source: BinaryIO) -> RecordBatchFileReader: ...
