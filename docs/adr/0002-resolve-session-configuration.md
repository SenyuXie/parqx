---
status: accepted
---

# Resolve configuration once for all sources in a session

A Parqx session can open sources from unrelated directories. Use the working directory at launch to choose project settings, so changing the order of the input files gives the same settings.

| Invocation | Settings applied in order, with later values overriding earlier values for the same key |
| --- | --- |
| Ordinary startup | Built-in defaults, `~/.parqx/config.toml` if present, the selected project file if present, then explicitly supplied CLI settings |
| `parqx --config PATH` | Built-in defaults, the file at `PATH`, then explicitly supplied CLI settings |

For ordinary startup, select the first project file present in the working directory, checking `./parqx.toml` before `./.parqx/config.toml`. A project file can set a query preview limit while inheriting a table preference from the user's file. The two project paths are alternative locations for the same project's settings.

An explicit file supplies the entire file-based configuration for the session. For example, if `team.toml` sets only a query preview limit, `parqx --config team.toml data.parquet` uses the built-in table preferences. This lets colleagues share a command and configuration file and reproduce the same settings across machines. uv uses this rule for its `--config-file` option. [uv configuration guide](https://docs.astral.sh/uv/concepts/configuration-files/)

## Initial settings

The following TOML shows all nine settings at their built-in defaults. A configuration file can specify any subset.

```toml
[query]
preview_rows = 10_000
preview_bytes = 33_554_432  # 32 MiB

[duckdb]
memory_limit = "256MB"      # Per connection
threads = 2

[table]
max_column_content_width = 48
cursor_type = "cell"
zebra_stripes = false
show_row_index = true
show_header = true
```

`query.preview_bytes` is a positive integer measured in bytes retained for a query preview. DuckDB accepts its own memory-size strings for `duckdb.memory_limit`. Both DuckDB settings apply per connection for source reads and SQL queries. Column content width is measured in terminal cells, excluding padding.

Use `--preview-rows`, `--preview-bytes`, `--memory-limit`, or `--threads` for one-off adjustments from the command line.

## Startup errors

An absent optional user or project file is skipped during discovery. An explicitly requested file must exist. If a selected file cannot be read, contains invalid TOML, or specifies an unknown key or invalid value, report the file path and the cause before opening the TUI. Include the key for setting errors. A malformed `./parqx.toml` ends startup even when `./.parqx/config.toml` exists.

## Table preferences and query previews

Apply table preferences when creating a table in a source tab or a query workspace. Keyboard shortcuts change the current table for its lifetime. Reusing that table for a subsequent query result preserves the user's display choices. Each newly created table starts from the session's configured preferences.

When a successful query produces a truncated preview, show one Textual information notification for that result using the default five-second timeout. Name the query workspace in the title, for example `Query 2 · Partial result`. Give the number of rows shown and the limit that caused truncation, for example `Showing 327 rows (32 MiB preview limit).` A complete result that happens to match a budget needs no notification.

Deliver the notification when the result is first visible. A visible result triggers it immediately. A result delivered to a background workspace triggers it when the user first views that result. Later visits to the same result do not repeat it. If a pending result is replaced or its workspace closes before it becomes visible, its notification expires with it. A one-time notification preserves table space and appears alongside the data it describes.
