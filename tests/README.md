# Tests

Run the complete suite with `uv run pytest -q`. Each behavior belongs at the
lowest layer that can verify it:

| Directory | Responsibility | CI coverage |
| --- | --- | --- |
| `unit/` | Source identity, Arrow data and memory bounds, SQL engine, CLI arguments, widget logic and shutdown coordination without mounting an app | Every test job |
| `integration/` | Mounted Textual widgets, multi-file workflows, input/focus, background reads and query lifecycle races | Linux / Python 3.12 |
| `smoke/` | Open a real Parquet file, navigate, run SQL, close a result and quit | Every test job |
| `distribution/` | Installed modules, metadata, console entry point, typing marker and Arrow/DuckDB integration | Isolated wheel and sdist jobs, including releases |

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
