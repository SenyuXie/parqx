# Parqx

Parqx is a lightweight terminal UI for browsing Apache Parquet files and running SQL queries, built with [Textual](https://github.com/Textualize/textual), [PyArrow](https://arrow.apache.org/docs/python/), and [DuckDB](https://github.com/duckdb/duckdb).

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

Press `Ctrl+P` and choose **SQL query** to show or hide the SQL panel.
Hiding it preserves your SQL and results.

Queries initially show a preview of up to 10,000 rows or approximately 32 MiB.
Press `F4` to load the rest. Cancellation and errors keep displayed rows available.

See the [DuckDB integration plan](docs/duckdb-integration.md) for implementation details.

## Keyboard control

### Navigation

| Key | Action |
| --- | --- |
| Arrow keys | Move the cursor |
| `PageUp` / `PageDown` | Move one page up / down |
| `Home` / `End` | Move to the first / last column |
| `Ctrl+Home` / `Ctrl+End` | Move to the first / last available row |
| `Enter` | Select the current cell |

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
| `Ctrl+P` → **SQL query** | Show / hide the SQL panel |
| `F1` | Run the complete SQL query |
| `F2` | Cancel the query or stop loading |
| `F3` | Browse the original file |
| `F4` | Load the complete result |
| `Escape` / `Shift+Tab` | Leave SQL input without cancelling |
| `Tab` | Indent SQL; enter SQL input from the table |
| `F6` / `F7` | Select the current SQL line / all SQL |

## License

Parqx is licensed under the MIT License. See [LICENSE](LICENSE) for details.

This project includes code derived from Textual; see [NOTICE](NOTICE).
