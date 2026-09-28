# Local PyArrow type declarations

`pyarrow/` contains the subset of PyArrow's API used by Parqx and its tests. These
stubs support strict static checking; they are not a complete model of PyArrow
and do not change its runtime behavior.

`pyproject.toml` points mypy's `mypy_path` and Pyright's `stubPath` at this directory.
The supported runtime dependency is declared there; `uv.lock` records the version
used by the development environment. Do not infer runtime support from a stub
alone.

## Updating a declaration

1. Check the installed PyArrow signature/docstring and, when needed, the matching
   version's official API documentation. Preserve keyword-only arguments, defaults,
   nullable results, overloads, and iterator element types that affect our calls.
2. Add the smallest accurate declaration needed by the source or tests. Prefer
   concrete Arrow types to broad `Any`; use `Any` only where values are inherently
   heterogeneous, such as `Scalar.as_py()`.
3. Exercise the runtime behavior in an existing or focused test. Type-checker
   success alone cannot verify a handwritten stub.
4. Run `.venv/bin/mypy src`, `.venv/bin/pyright --pythonpath .venv/bin/python`,
   Ruff lint/format, and the relevant pytest tests.

When upgrading PyArrow, recheck the APIs we use before relaxing types to silence
new diagnostics. If suitable upstream type declarations become available,
prefer them and remove redundant local definitions after verifying both checkers.

`parqx/py.typed` marks Parqx's own inline annotations for consumers; it does not
turn this local PyArrow subset into a separately supported stub package.
