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

Press `ctrl+p` → **SQL query** to query the file as `data`. Results open in new tabs.

Queries accept one `SELECT` statement, including `WITH`. Previews show up to
10,000 rows or about 32 MiB of Arrow data; a single oversized row may exceed
the byte limit.

## Keyboard control

### Navigation

| Key                   | Action                        |
| ---                   | ---                           |
| `↑` / `↓`             | Move the cursor up or down    |
| `←` / `→`             | Move the cursor left or right |
| `pageUp` / `pageDown` | Move one page up or down      |
| `home`                | Move to the leftmost column   |
| `end`                 | Move to the rightmost column  |
| `ctrl+home`           | Move to the first row         |
| `ctrl+end`            | Move to the last row          |
| `enter`               | Select the current cell       |

### Table View

These shortcuts apply to the active tab.

| Key      | Action                                               |
| ---      | ---                                                  |
| `h`      | Toggle the column header row                         |
| `i`      | Toggle the row-index column                          |
| `z`      | Toggle zebra striping                                |
| `c`      | Cycle cursor type (cell → row → column → none)       |
| `ctrl+w` | Close the current tab, keeping at least one tab open |

### SQL query

| Key                      | Action                                        |
| ---                      | ---                                           |
| `ctrl+p` → **SQL query** | Open the centered SQL editor                  |
| `enter`                  | Run the complete SQL query                    |
| `shift+enter`            | Insert a newline                              |
| `esc`                    | Close the editor and cancel any running query |
| `ctrl+w`                 | Delete the previous word in the SQL editor    |
| `f6` / `f7`              | Select the current SQL line / all SQL         |

## License

Parqx is licensed under the MIT License. See [LICENSE](LICENSE) for details.

This project includes code derived from Textual; see [NOTICE](NOTICE).
