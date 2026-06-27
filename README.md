# Parqx

Parqx is a lightweight terminal UI for inspecting Apache Parquet files.

It opens a local Parquet file directly in your terminal and displays it as an interactive, scrollable table backed by PyArrow and Textual.

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

## Navigation

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
| `Ctrl+Q`              | Quit                          |

## License

Parqx is licensed under the MIT License. See [LICENSE](LICENSE) for details.
