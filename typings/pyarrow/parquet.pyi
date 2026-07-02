from __future__ import annotations

from collections.abc import Sequence
from os import PathLike
from typing import Any, BinaryIO

from . import DataType, NativeFile, Schema, Table

class RowGroupMetaData:
    @property
    def num_rows(self) -> int: ...

class FileMetaData:
    @property
    def num_row_groups(self) -> int: ...
    def row_group(self, i: int) -> RowGroupMetaData: ...

class ParquetFile:
    def __init__(
        self,
        source: str | PathLike[str] | NativeFile | BinaryIO,
        *,
        metadata: FileMetaData | None = None,
        common_metadata: FileMetaData | None = None,
        read_dictionary: Sequence[str] | None = None,
        binary_type: DataType | None = None,
        list_type: DataType | None = None,
        memory_map: bool = False,
        buffer_size: int = 0,
        pre_buffer: bool = False,
        coerce_int96_timestamp_unit: str | None = None,
        decryption_properties: Any | None = None,
        thrift_string_size_limit: int | None = None,
        thrift_container_size_limit: int | None = None,
        filesystem: Any | None = None,
        page_checksum_verification: bool = False,
        arrow_extensions_enabled: bool = True,
    ) -> None: ...
    @property
    def metadata(self) -> FileMetaData: ...
    @property
    def schema_arrow(self) -> Schema: ...
    @property
    def closed(self) -> bool: ...
    def close(self, force: bool = False) -> None: ...
    def read_row_group(
        self,
        i: int,
        columns: Sequence[str] | None = None,
        use_threads: bool = True,
        use_pandas_metadata: bool = False,
    ) -> Table: ...

def read_table(
    source: str | PathLike[str] | NativeFile | BinaryIO,
    *,
    columns: Sequence[str] | None = None,
    use_threads: bool = True,
    schema: Schema | None = None,
    use_pandas_metadata: bool = False,
    read_dictionary: Sequence[str] | None = None,
    binary_type: DataType | None = None,
    list_type: DataType | None = None,
    memory_map: bool = False,
    buffer_size: int = 0,
    partitioning: str | Sequence[str] = "hive",
    filesystem: Any | None = None,
    filters: Any | None = None,
    ignore_prefixes: Sequence[str] | None = None,
    pre_buffer: bool = True,
    coerce_int96_timestamp_unit: str | None = None,
    decryption_properties: Any | None = None,
    thrift_string_size_limit: int | None = None,
    thrift_container_size_limit: int | None = None,
    page_checksum_verification: bool = False,
    arrow_extensions_enabled: bool = True,
) -> Table: ...
def write_table(
    table: Table,
    where: str | PathLike[str] | NativeFile | BinaryIO,
    row_group_size: int | None = None,
    **kwargs: Any,
) -> None: ...
