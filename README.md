# Parqx

Parqx is a lightweight terminal UI for inspecting Apache Parquet files, built on top of [Textual](https://github.com/Textualize/textual) and [PyArrow](https://arrow.apache.org/docs/python/).

Parqx opens local Parquet files directly in the terminal and displays them with ArrowTable, an interactive, scrollable, Arrow-backed widget purpose-built for inspecting Parquet data rather than wrapping Textual's general-purpose [DataTable](https://textual.textualize.io/widget_gallery/#datatable).

![Parqx app screenshot](https://github.com/user-attachments/assets/2df09bca-9ee6-423d-a4dd-dac0e9297cc6)

## Installation

Parqx requires Python 3.12 or newer.

```bash
pip install parqx
```

## Usage

Open a Parquet file:

```bash
parqx data/weather.parquet
```

Run SQL directly against the file, available as the `data` view:

```bash
parqx data/weather.parquet --query 'SELECT count(*) AS rows FROM data'
```

Press `Ctrl+P` and choose **SQL query** to show or hide the bottom SQL panel.
The command's description changes between **Show the SQL query** and
**Hide the SQL query**. Hiding the panel retains its SQL, selection, undo history,
and the displayed result. Press `F1` to execute the complete SQL query.
`F2` cancels execution, and `F3` returns to the original file. Query errors
keep the previous result visible. The initial preview is limited to 10,000 rows
and approximately 32 MiB of Arrow data; the status line identifies truncated
results. Aggregations still operate on the full input. A single oversized value
and engine/decoder allocations can exceed the preview budget.

Press `F4` to continue the same query beyond the preview. Results are
written to temporary Arrow batches and read back as you navigate. While loading,
the status shows available rows; `Ctrl+End` goes to the currently available end.
The total becomes known when loading completes. `F2` keeps the displayed
prefix. Temporary results are removed when replaced or when the app closes.

The SQL query panel uses keyboard controls without buttons. In the input area,
`Enter` inserts a newline and `Tab` indents. `Escape` or `Shift+Tab` moves focus
back to the result table without cancelling the query; `Tab` from the table
returns to the input area. Native TextArea editing shortcuts remain available,
including `F6` to select a line, `F7` to select all, `Ctrl+Z` to undo, and `Ctrl+Y`
to redo. `Ctrl+Enter` and `F5` do not execute SQL.

Original file browsing reads metadata first and loads row windows on demand,
with a 32 MiB Arrow cache and a small width sample. Large Parquet row groups may
still require decoding preceding batches when jumping far into a group. The
engine uses a separate 256 MB DuckDB memory budget (not a process-wide hard cap).

SQL results use DuckDB's types; original browsing retains the file's Arrow types.
The architecture and staged implementation are tracked in
[the DuckDB integration plan](docs/duckdb-integration.md).

## Keyboard control

### Navigation

| Key                   | Action                        |
| ---                   | ---                           |
| `↑` / `↓`             | Move the cursor up or down    |
| `←` / `→`             | Move the cursor left or right |
| `PageUp` / `PageDown` | Move one page up or down      |
| `Home`                | Move to the leftmost column   |
| `End`                 | Move to the rightmost column  |
| `Ctrl+Home`           | Move to the first row         |
| `Ctrl+End`            | Move to the last row          |
| `Enter`               | Select the current cell       |

### Table View

| Key | Action                                         |
| --- | ---                                            |
| `H` | Toggle the column header row                   |
| `I` | Toggle the row-index column                    |
| `Z` | Toggle zebra striping                          |
| `C` | Cycle cursor type (cell → row → column → none) |

### SQL query

| Key | Action |
| --- | --- |
| `Ctrl+P` | Open the command palette; **SQL query** shows or hides the panel |
| `F1` | Run the complete SQL query |
| `F2` | Cancel execution, stop loading, or release a paused preview |
| `F3` | Browse the original file |
| `F4` | Continue loading the complete query result |
| `Escape` / `Shift+Tab` | Leave the SQL input without cancelling the query |
| `Tab` | Indent in the SQL input; focus the input from the result table |
| `F6` / `F7` | Select the current line / all text in the SQL input |

## License

Parqx is licensed under the MIT License. See [LICENSE](LICENSE) for details.

This project includes code derived from Textual; see [NOTICE](NOTICE).
