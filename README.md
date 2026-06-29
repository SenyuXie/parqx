# Parqx

Parqx is a lightweight terminal UI for inspecting Apache Parquet files.

Parqx opens a local Parquet file directly in terminal and displays it as an interactive, scrollable table backed by PyArrow and Textual.

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
