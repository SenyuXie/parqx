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

These display shortcuts apply while a table has focus.

| Key      | Action                                               |
| ---      | ---                                                  |
| `h`      | Toggle the column header row                         |
| `i`      | Toggle the row-index column                          |
| `z`      | Toggle zebra striping                                |
| `c`      | Cycle cursor type (cell → row → column → none)       |
| `ctrl+w` | Close the current tab, keeping at least one tab open |

### SQL query

| Key                      | Action                                                 |
| ---                      | ---                                                    |
| `ctrl+p` → **Query** | Open a new blank query workspace |
| `ctrl+enter` | Run the active workspace's complete SQL draft |
| `enter` | Insert a newline in the editor |
| `tab` | Indent in the editor |
| `shift+tab` | Move focus backward |

Each workspace keeps its draft and latest successful result while you switch
between tabs. Run again to replace its result in place. One query can run at a
time; its draft stays read-only until completion, while other tabs remain usable.
All ready sources stay queryable when their file views are closed. Workspaces
last for the current session only.

### Sources

Use `ctrl+p` → **Toggle Sources** to show or hide the shared sources sidebar.
It lists each session file, its quoted SQL table name, loading or failure state,
and loaded columns and types. Expand with `→`, collapse with `←`, and use native
`Tab` / `Shift+Tab` focus navigation. Long labels can be inspected with the tree's
horizontal scrollbar. Inspecting the tree does not change SQL text or table
display settings.

Closing a file tab releases its cached rows while keeping its schema and SQL
identity available for the session. Metadata that finishes after a tab closes
updates the sidebar without opening another file view.

## License

Parqx is licensed under the MIT License. See [LICENSE](LICENSE) for details.

This project includes code derived from Textual; see [NOTICE](NOTICE).
