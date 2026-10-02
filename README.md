# Parqx

Parqx is a lightweight terminal UI for inspecting Apache Parquet files and running SQL queries, built on top of [Textual](https://github.com/Textualize/textual), [PyArrow](https://arrow.apache.org/docs/python/), and [DuckDB](https://github.com/duckdb/duckdb).

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

The file opens in its own tab. Press `Ctrl+P` and choose **SQL query** to open
a centered SQL editor. The original file is available as the `data` view.
Press `Enter` to run the complete SQL query, or `Shift+Enter` to insert a newline.
Press `Esc` to close the editor and cancel any running query. Reopening the
editor preserves its text, selection, and undo history.

Successful queries open in new tabs. Errors remain in the editor so you can
correct the SQL and try again. Cancellation and errors leave existing tabs
available.

Queries support a single `SELECT` statement, including `WITH` queries. Results
are previews of up to 10,000 rows or approximately 32 MiB of Arrow data; a single
oversized row may exceed the byte budget. These limits apply to the output,
so aggregates still use all matching input rows. Query execution and decoding
use additional memory.

Each result tab shows its row count and execution time. Its status line indicates
when a preview is truncated and the total row count is unknown. Refine the SQL
to inspect other rows.

Press `Ctrl+W` to close the current tab. The footer shows this shortcut and
disables it when only one tab remains. The original file tab can also be closed
while other tabs are open; SQL queries still use the original file through
`data`. Each tab preserves its own table position and display settings.

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

These shortcuts apply to the active tab.

| Key | Action                                         |
| --- | ---                                            |
| `H` | Toggle the column header row                   |
| `I` | Toggle the row-index column                    |
| `Z` | Toggle zebra striping                          |
| `C` | Cycle cursor type (cell → row → column → none) |
| `Ctrl+W` | Close the current tab, keeping at least one tab open |

### SQL query

| Key | Action |
| --- | --- |
| `Ctrl+P` → **SQL query** | Open the centered SQL editor |
| `Enter` | Run the complete SQL query |
| `Shift+Enter` | Insert a newline |
| `Esc` | Close the editor and cancel any running query |
| `Tab` | Indent SQL |
| `Ctrl+W` | Delete the previous word in the SQL editor |
| `F6` / `F7` | Select the current SQL line / all SQL |

## License

Parqx is licensed under the MIT License. See [LICENSE](LICENSE) for details.

This project includes code derived from Textual; see [NOTICE](NOTICE).
