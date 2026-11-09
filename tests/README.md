# Tests

Before publishing changes, run the same checks as CI from the repository root:

```sh
uv run --locked poe check
```

Poe tasks in `pyproject.toml` define the commands shared with CI. `check` runs
lint/format checks, both type checkers, the full test suite, and isolated
wheel/sdist smoke tests, stopping at the first failure. Package checks use
`scripts/check_package.py` to build into a temporary directory so previous
artifacts cannot affect the result.

For one CI job, replace `check` with `lint`, `typecheck`, `full`, `core`, or
`package`. By default, `full` runs all tests and `core` runs unit and smoke
tests. Both accept additional pytest arguments, for example
`uv run --locked poe full -k value_browser`. `typecheck` covers application code,
tests, and helper scripts. Poe and the project checks use the locked development
environment. Run `check` before publishing even when focused checks were
sufficient during development.

## Coverage reports

Run `uv run --locked poe coverage` from the repository root to measure statement
and branch coverage for `parqx` across the unit, integration, and smoke tests.
The terminal report lists missing lines and branch destinations for each module.
Open `htmlcov/index.html` to inspect highlighted source lines and partial
branches, or use `coverage.xml` for tools that consume Cobertura XML. The same
test run produces all three reports. Use `poe full` or `poe check` for routine
validation and `poe coverage` when you need a report.

## Test layout

Each behavior belongs at the lowest layer that can verify it:

| Directory | Responsibility | CI coverage |
| --- | --- | --- |
| `unit/` | Source identity, Arrow data and memory bounds, SQL engine, CLI arguments, widget logic and native resource cleanup without mounting an app | Every test job |
| `integration/` | Mounted Textual widgets, multi-file workflows, input/focus, background reads and query lifecycle races | Linux / Python 3.12 |
| `smoke/` | Open a real Parquet file, navigate, run SQL, confirm workspace closure and quit | Every test job |
| `distribution/` | Installed modules, metadata, console entry point, typing marker and Arrow/DuckDB integration | Isolated wheel and sdist jobs, including releases |

Place unit tests beside tests of the same application package. The `data/`,
`query/`, and `tui/` directories under `unit/` mirror `src/parqx/`. Tests for
top-level modules such as the catalog, CLI, and configuration live directly
under `unit/`.

Find mounted tests by the feature under test:

| Integration path | What belongs here |
| --- | --- |
| `integration/sources/` | Source discovery, tree navigation, reopening files, and page reads |
| `integration/query/` | SQL editing, execution, feedback, workspace layout, and close confirmation |
| `integration/values/` | Value selection, nested navigation, rendering, large values, and inspection across workspaces |
| `integration/` | Cross-feature scenarios such as application budget wiring and the empty workspace |

Keep fixtures in the nearest `conftest.py` shared by their consumers. The root
`conftest.py` supplies the small Parquet file used by all three pytest suites.
`integration/values/conftest.py` owns the specialized Arrow and Parquet inputs
for value inspection. `helpers.py` holds UI actions shared with the smoke test.
Run a feature group directly, for example
`uv run --locked pytest -q tests/integration/values`.

## What to test at each layer

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

`value_document` indexes direct-child positions and wrapped UTF-8 text in bounded
batches bounded by source bytes as well as visual rows. Oversized combining
sequences continue across visual rows without truncating the raw Unicode; terminal
controls are displayed as visible escapes. Binary rows are addressed directly in
the retained Arrow buffer. Only
compact offsets and the current decoded text page are retained; no formatted
history or child widgets are allocated for every value. Check complete content,
Unicode boundaries and allocation bounds here. `ValueChildren` and `ScalarReader`
use Textual's line rendering API for the visible viewport. Their owning browser
preserves selection and pending source positions, while cancellable index workers pause
when hidden and reject results for replaced documents. Exercise those lifecycles
through the real app, delaying `index_document` with events when a race matters.

The compatibility matrix covers Linux on Python 3.12 and 3.13, plus macOS and
Windows on Python 3.12. Detailed UI scenarios run once; the other jobs run
`uv run --locked poe core`. This checks operating-system and
Python-version compatibility without repeating the full UI suite for every pair.

For focused work, run `uv run pytest -q tests/unit` or
`uv run pytest -q tests/integration`. Add `--durations=15` to inspect expensive
scenarios. Use `uv run --locked poe package` to repeat the
isolated distribution checks.

Test modules must not import other test modules. Each UI test owns its application and uses
observable-state waits. Coordinate background races with events, release them in
`finally` blocks, and avoid fixed sleeps. Combine closely related assertions in a
single user journey, but keep independent failure modes in separate tests.

Add a regression at the layer that owns the behavior. Data-type and budget
boundaries belong in unit tests; an integration test should verify the UI wiring
without repeating the same input matrix. Retired shortcuts, old layout details
and historical internal names do not need permanent negative tests.
