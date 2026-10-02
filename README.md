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

Press `Ctrl+P` and choose **SQL query** to show or hide the SQL editor.
The current file is available as the `data` view. Hiding the editor preserves its
text and the displayed results.

Start with a query instead of loading the original table:

```bash
parqx data/weather.parquet --query 'SELECT count(*) AS rows FROM data'
```

Queries support a single `SELECT` statement, including `WITH` queries. Results
are previews of up to 10,000 rows or approximately 32 MiB of Arrow data; a single
oversized row may exceed the byte budget. These limits apply to the output,
so aggregates still use all matching input rows. Query execution and decoding
use additional memory.

The status line indicates when a preview is truncated and its total row count
is unknown. Refine the SQL to inspect other rows. Cancellation and errors keep
the displayed results available; `F3` returns to the original file's values.

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
| `Ctrl+P` → **SQL query** | Show / hide the SQL editor |
| `F1` | Run the complete SQL query |
| `F2` | Cancel the running query |
| `F3` | Browse the original file |
| `Escape` / `Shift+Tab` | Leave SQL input without cancelling |
| `Tab` | Indent SQL; enter SQL input from the table |
| `F6` / `F7` | Select the current SQL line / all SQL |

## License

Parqx is licensed under the MIT License. See [LICENSE](LICENSE) for details.

This project includes code derived from Textual; see [NOTICE](NOTICE).
