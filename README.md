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

Press `ctrl+p` → **SQL query** to query the file by its filename without the extension (for example, `"weather"`). Results open in new tabs.

Open several files at once, each in its own tab:

```bash
parqx users.parquet orders.parquet
parqx data/*.parquet
```

Your shell expands the wildcard. Tabs follow argument order, and repeated paths or symbolic links to the same file open only once. Each tab keeps its own cursor and display settings. Hover over a file tab to see its full path.

### Query across files

The file status bar shows its quoted SQL name. For `users.parquet` and `orders.parquet`, for example:

```sql
SELECT u.name, sum(o.amount) AS total
FROM "users" AS u
JOIN "orders" AS o ON u.id = o.user_id
GROUP BY u.name
ORDER BY total DESC
```

Run one `SELECT` statement at a time; CTEs (`WITH`) and `UNION` are supported. Queries read the complete files, independently of which rows you have browsed. The result preview is limited to 10,000 rows or 32 MiB, with an exception for a single oversized row; this does not limit the input to joins or aggregations. Result tabs are not added as SQL tables.

Table names keep the filename without its final extension. Use double quotes for names containing spaces, dots, keywords or other special characters, and double any embedded quote:

| File | SQL table name |
| --- | --- |
| `sales.2026.parquet` | `"sales.2026"` |
| `monthly sales.parquet` | `"monthly sales"` |
| `订单.parquet` | `"订单"` |
| `a"b.parquet` | `"a""b"` |
| `select.parquet` | `"select"` |

Names that differ only in ASCII letter case conflict. Files with conflicting names receive `_2`, `_3`, and so on, skipping names already assigned. For example, opening `east/sales.parquet` then `west/sales.parquet` creates `"sales"` and `"sales_2"`. Names are assigned in argument order and stay fixed even if loading fails or a tab closes. File status bars show the exact names to use.

**Migration:** the automatic `data` alias has been removed, including for single-file sessions. Replace `FROM data` with the displayed filename-based table name. A file actually named `data.parquet` still uses `"data"`.

### Loading and closing files

Each query captures the sources that have finished loading when you press `enter`. Loading or failed sources are excluded and reported as source warnings. Sources that finish loading during a query are available on the next run. Even with no ready sources, expressions such as `SELECT 42` work.

If a previously loaded file becomes unreadable, queries that do not depend on it can still succeed with a warning. Restore the file and run the query again to retry. A read error during execution produces an error instead of a partial result.

Closing a file tab releases its browsing cache while keeping the file available to SQL. If metadata is still loading, it can finish and make the source available without reopening the tab. At least one tab remains open. Press `esc` in the editor to cancel a running query; reopening it preserves your SQL and edit history.

If some input files fail to open, their tabs show the path and reason while the other files remain usable. On exit, Parqx prints the input errors and returns status `1`. If every input fails, it exits automatically with status `1`; a normal session without input errors returns `0`.

File browsing reads windows of rows and retains up to 32 MiB per open source, allowing a single oversized row. This is a per-source budget: total memory can grow with the number of tabs. Open all desired files at startup; adding files during a session or automatically combining partitions is not supported.

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

## License

Parqx is licensed under the MIT License. See [LICENSE](LICENSE) for details.

This project includes code derived from Textual; see [NOTICE](NOTICE).
