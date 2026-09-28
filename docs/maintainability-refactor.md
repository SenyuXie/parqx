# Maintainability refactor plan

Base: `codex/duckdb-query` at `d4008a3`. Work branch:
`codex/maintainability-refactor`, intended to merge into `codex/duckdb-query`.

## Scope and invariants

Improve the whole codebase's readability without changing the product's behavior.
The separate PR-review findings (literal DuckDB paths, width sampling, error-label
markup, and the removed benchmark) are outside this refactor.

- Keep rendering synchronous and cache-only; perform file and SQL I/O on workers.
- Execute SQL once. A truncated preview resumes the same reader on Load all.
- Preserve cancellation, stale-request rejection, editor focus, and the old result
  while a replacement query is running or fails.
- Make result ownership explicit: execution owns a new store until the UI accepts
  it, after which the displayed view owns cleanup, including during shutdown.
- Preserve Arrow nulls, timestamp precision, bounded pages, and the one-oversized-row
  exception that guarantees progress.
- Keep Textual message names, key bindings, and the third-party attribution intact.
- Keep the CLI, logging, cell formatter, and query panel simple; do not introduce
  abstractions solely to reduce file length.

## Commit sequence

Each implementation step is a separate, reviewable commit. Update this checklist
and record validation as the work lands.

1. [x] Record this plan and establish the new branch.
2. [x] **Shared test support.** Move asynchronous waiting and command-palette
   navigation into `tests/helpers.py`; replace cross-test imports. Add a small
   worker gate with guaranteed release for concurrency tests, preserving their
   assertions and real scheduling boundaries.
3. [x] **Bounded data windows and contracts.** Share page cropping, byte accounting,
   compaction, and assembly between Parquet and IPC sources. Keep their I/O and
   locking separate. Move read cancellation to the shared data contract, clarify
   prefix/count names, type preview-limit reasons, and name independent budgets.
   Verify cross-batch windows, byte limits, cancellation, and oversized rows.
4. [x] **Single-line table rendering.** Return flat cell segments and explicitly
   separate fixed and scrollable row segments. Simplify cell-style inputs to
   keyword-only per-cell flags. Give formatted-cell, rendered-line, and layout
   invalidation clear boundaries. Trim repetitive documentation while retaining
   coordinate, buffer-ownership, cache, scheduling, and public-message contracts.
   Verify rendering, cursor/hover styles, data replacement, and lazy navigation.
5. [x] **Query phases.** Replace independently mutable UI booleans with one query
   phase and derived action availability. Keep worker control events separate
   from UI state and the currently displayed data. Verify preview, Load all,
   cancellation, failure, supersession, and modal keyboard behavior.
6. [x] **Query execution.** Extract preview/pause/materialization/progress into the
   query layer with typed callbacks and explicit synchronous store handoff. Keep
   Textual workers and request checks in the app. Name progress and shutdown
   timing constants. Verify accepted/rejected ownership, cleanup, and errors.
7. [x] **Architecture documentation and final verification.** Replace the old
   integration proposal with current threading, data flow, ownership, cache, and
   testing documentation. Document the deliberately partial PyArrow stubs.
   Review the final diff for unnecessary abstractions and behavior changes.

## Validation

Run focused tests for each implementation step, then the complete gates before
committing it. Documentation-only steps need whitespace checks, not repeated tests.

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pyright --pythonpath .venv/bin/python
.venv/bin/mypy src
.venv/bin/python -m pytest -q
git diff --check
```

The explicit Python path makes Pyright use this checkout's dependency environment.
Tests that exercise ownership or thread transitions must use controlled events,
not arbitrary sleeps. Add tests for meaningful boundaries introduced by the
refactor; do not duplicate implementation details in assertions.

## Validation log

- Baseline: Ruff lint/format, Pyright, mypy passed; pytest: 83 passed.
- Shared test support: all four gates passed; pytest: 83 passed.
- Data windows/contracts: all four gates passed; pytest: 97 passed, including
  shared backend boundary cases and single-oversized-row progress.
- Table rendering: all four gates passed; pytest: 99 passed. Verified ellipsis,
  terminal-cell widths, mouse metadata, cursor/hover visibility, and format-cache reuse.
- Query phases: all four gates passed; pytest: 99 passed. Existing integration
  tests now also check phases at controlled execution, preview, full-load, and stop boundaries.
- Query execution: all four gates passed; pytest: 104 passed. New tests verify
  accepted/rejected handoff, failures before/after acceptance, and preview cancellation.
- Final review: public Textual message implementations, key bindings, component
  classes, and CSS are unchanged. No unrelated source changes or dependency updates.
- Final source gates: Ruff lint/format, Pyright, mypy passed; pytest: 104 passed.
- Packaging: wheel and sdist built successfully; both passed the distribution
  smoke check in isolated environments with lockfile runtime dependencies. The
  initial offline install lacked cached dependencies; the locked environment was
  then prepared and both artifact checks completed successfully.
- Documentation: current architecture and partial PyArrow-stub maintenance guide
  completed; whitespace checks passed. No source changes after the final gates.

## Handoff

The seven commits follow the sequence above. Open the refactor PR with
`codex/maintainability-refactor` as the head and `codex/duckdb-query` as the base.
After that PR merges, the DuckDB feature branch can be reviewed separately for
its eventual merge into `master`.
