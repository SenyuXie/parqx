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

Press `F2` to open the SQL editor, then `F5` or `Ctrl+Enter` to run a query.
`Escape` cancels execution, and `F6` returns to the original file. Query errors
keep the previous result visible. The initial preview is limited to 10,000 rows
and approximately 32 MiB of Arrow data; the status line identifies truncated
results. Aggregations still operate on the full input. A single oversized value
and engine/decoder allocations can exceed the preview budget.

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

## License

Parqx is licensed under the MIT License. See [LICENSE](LICENSE) for details.

This project includes code derived from Textual; see [NOTICE](NOTICE).
