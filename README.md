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

| Key                   | Action                          |
| ---                   | ---                             |
| `↑` / `↓`             | Move the cursor up or down      |
| `←` / `→`             | Move the cursor left or right   |
| `pageUp` / `pageDown` | Move one page up or down        |
| `home`                | Move to the leftmost column     |
| `end`                 | Move to the rightmost column    |
| `ctrl+home`           | Move to the first row           |
| `ctrl+end`            | Move to the last row            |
| `enter`               | Inspect the current cell or row |
| `ctrl+w`              | Close the current tab           |

### Table View

These display shortcuts apply while a table has focus.

| Key | Action                                         |
| --- | ---                                            |
| `h` | Toggle the column header row                   |
| `i` | Toggle the row-index column                    |
| `z` | Toggle zebra striping                          |
| `c` | Cycle cursor type (cell → row → column → none) |
| `v` | Show or hide the selected value sidebar        |

### Value browser

Press `tab` from the table to focus the open sidebar.

| Key                   | Action                               |
| ---                   | ---                                  |
| `↑` / `↓`             | Select the previous or next item     |
| `home` / `end`        | Select the first or last item        |
| `pageUp` / `pageDown` | Move one page up or down             |
| `enter`               | Open the selected item               |
| `backspace`           | Return to the parent (table at root) |
| `esc`                 | Return focus to the table            |

## License

Parqx is licensed under the MIT License. See [LICENSE](LICENSE) for details.

This project includes code derived from Textual; see [NOTICE](NOTICE).
