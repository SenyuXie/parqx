# Parqx

Parqx is a terminal application for inspecting Parquet files and exploring data through SQL.

## Language

**Query workspace**:
A workspace that pairs SQL text with its query result for repeated editing, execution, and inspection. Each query workspace is independent.

**SQL draft**:
The editable SQL text in a query workspace. It may differ from the SQL that produced the current result.

**Current result**:
The data from a query workspace's most recent successful execution. Editing the SQL draft or a subsequent execution failure preserves this data; a successful execution returning zero rows replaces it with an empty result.

**Row value**:
A table row treated as a special Struct value whose fields correspond to the row's columns. Row values and cell values share the same value browsing model.

**Browse root**:
A cell value or row value explicitly selected as the starting point for value browsing. It may differ from the value currently under the table cursor.

**Browse path**:
The sequence of fields or elements traversed from the browse root to the value currently being inspected.
