# Parqx

Parqx is a lightweight terminal UI for inspecting Apache Parquet files.

![Parqx screenshot](https://github.com/user-attachments/assets/ff7d1f08-1f64-4f9d-a22f-c171f8bbf20a)

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
| `enter`               | Inspect the current cell or row |
| `ctrl+w`              | Close the current tab         |

### Table View

These display shortcuts apply while a table has focus.

| Key | Action                                         |
| --- | ---                                            |
| `h` | Toggle the column header row                   |
| `i` | Toggle the row-index column                    |
| `z` | Toggle zebra striping                          |
| `c` | Cycle cursor type (cell → row → column → none) |
| `v` | Show or hide the selected value sidebar         |

### Value browser

In file tables and query results, `enter` selects a cell or row for the right
sidebar. Moving the table cursor leaves that selection unchanged. A row is shown
as ordered fields; Struct, List, and Map values can be explored one level at a
time. Map entries expose separate `key` and `value` fields.

| Key or action | Behavior |
| --- | --- |
| `tab` from the table | Focus the open sidebar |
| `↑` / `↓`, `home` / `end`, `pageUp` / `pageDown` | Navigate container children |
| `enter` or click a child | Open that child in the sidebar |
| `backspace` or click an ancestor breadcrumb | Return to the parent or ancestor and restore its reading position |
| `esc`, or `backspace` at the root | Return focus to the table |
| `v` in the table or sidebar | Hide or reopen the sidebar, preserving its path and reading position |

Previews may be shortened; entering a supported scalar opens its complete,
read-only text. A new Enter selection starts a fresh path. A successful query
clears the previous result's selection until another value is selected.

## License

Parqx is licensed under the MIT License. See [LICENSE](LICENSE) for details.

This project includes code derived from Textual; see [NOTICE](NOTICE).
