# Tests

Run the complete suite with `uv run pytest -q`. Each behavior belongs at the
lowest layer that can verify it:

| Directory | Responsibility | CI coverage |
| --- | --- | --- |
| `unit/` | Source identity, Arrow data and memory bounds, SQL engine, CLI arguments, widget logic and native resource cleanup without mounting an app | Every test job |
| `integration/` | Mounted Textual widgets, multi-file workflows, input/focus, background reads and query lifecycle races | Linux / Python 3.12 |
| `smoke/` | Open a real Parquet file, navigate, run SQL, confirm workspace closure and quit | Every test job |
| `distribution/` | Installed modules, metadata, console entry point, typing marker and Arrow/DuckDB integration | Isolated wheel and sdist jobs, including releases |

`parqx.config.ParqxConfig` defines immutable application budgets. The app passes
one instance to its components; native data helpers receive explicit settings.
Configuration validation belongs in unit tests, and budget wiring belongs in
mounted integration tests.

`data.duckdb` provides shared connection setup and bounded Arrow
previews, including private batch budgeting and compaction helpers.
`data.parquet` reads file metadata and browsing windows through DuckDB;
each request owns a short-lived connection. Paths are passed directly to DuckDB;
callers supply paths that DuckDB can read without escaping or aliases.
`query.engine.execute_query` owns SQL execution, timing, source issues and errors
on the same shared primitives. Loaded results remain native `pa.Table` objects.
`PageCache` only holds source
metadata and bounded `DataPage` windows. `ArrowTable.replace_data` accepts either
kind and resets navigation and rendering when data changes. Keep paging order,
type conversions, budgets and resource cleanup tests at
these boundaries; distribution checks exercise both browsing and SQL.

`TablePane` displays loaded Arrow data or a source awaiting metadata. Source tabs
own one submitted page read and one latest pending window, using Textual timers
for trailing debounce; every tab releases cached data on unmount. The app owns
source metadata loading and the catalog, so closing a tab does not remove a source
from SQL. `QueryWorkspace` owns its SQL draft and most recent successful result.
The app owns one Textual thread worker and delivers its result to the submitting
workspace on the UI thread. That draft stays read-only and cannot close until
delivery; other workspaces and source views remain usable. Completion never
changes the active tab or focus.
Native reads finish naturally after UI exit, with no interrupt or shutdown
deadline. Cover these ownership and lifecycle boundaries in integration
tests, using events to coordinate native reads.

The compatibility matrix covers Linux on Python 3.12 and 3.13, plus macOS and
Windows on Python 3.12. Detailed UI scenarios run once; the other jobs run
`uv run pytest -q tests/unit tests/smoke`. This checks both operating-system and
Python-version compatibility without repeating the full UI suite for every pair.

For focused work, run `uv run pytest -q tests/unit` or
`uv run pytest -q tests/integration`. Add `--durations=15` to inspect expensive
scenarios. Package checks run separately against built artifacts:

```sh
uv build
uv run --isolated --no-project --with dist/*.whl tests/distribution/check_install.py
uv run --isolated --no-project --with dist/*.tar.gz tests/distribution/check_install.py
```

Keep fixtures in `conftest.py` and shared UI actions in `helpers.py`; test modules
must not import other test modules. Each UI test owns its application and uses
observable-state waits. Coordinate background races with events, release them in
`finally` blocks, and avoid fixed sleeps. Combine closely related assertions in a
single user journey, but keep independent failure modes in separate tests.

Add a regression at the layer that owns the behavior. Data-type and budget
boundaries belong in unit tests; an integration test should verify the UI wiring
without repeating the same input matrix. Retired shortcuts, old layout details
and historical internal names do not need permanent negative tests.
