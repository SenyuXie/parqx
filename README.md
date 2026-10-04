# Parqx

Parqx is a lightweight terminal UI for inspecting Apache Parquet files.

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

Open several files at once:

```bash
parqx users.parquet orders.parquet
parqx data/*.parquet
```

## Browsing and types

Parqx uses DuckDB for browsing and SQL. Browsing loads a small window of rows
at a time and caches it for nearby navigation.

Displayed schemas and values follow DuckDB's interpretation of the file.
Timezone-aware nanosecond timestamps are read at microsecond precision, and
some Arrow extension types are converted. Browsing rejects files containing
decimal columns with precision above 38 to avoid incorrect conversion.
Browsing does not modify the original files.

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
| `ctrl+p` → **SQL query** | Open the compact SQL editor                   |
| `enter`                  | Run the complete SQL query                    |
| `shift+enter`            | Insert a newline and expand the editor        |
| `esc`                    | Close the editor and cancel any running query |

## License

Parqx is licensed under the MIT License. See [LICENSE](LICENSE) for details.

This project includes code derived from Textual; see [NOTICE](NOTICE).
