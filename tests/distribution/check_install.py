"""Smoke test for a built parqx distribution.

Run against an installed wheel or sdist (NOT the source tree) to catch
packaging-time regressions that the pytest suite cannot see: a missing
submodule, a broken `[project.scripts]` entry point, an unshipped
`py.typed` marker, or a runtime dependency that was only available in dev.

Invoked from the CI and release workflows:

    uv run --isolated --no-project --with dist/*.whl tests/distribution/check_install.py
    uv run --isolated --no-project --with dist/*.tar.gz tests/distribution/check_install.py

The `--isolated --no-project` flags mean the only things available are the
standard library, the built parqx artifact, and parqx's declared runtime
dependencies. Do not import pytest or any other dev-only package here.
"""

from __future__ import annotations

import subprocess
import tomllib
from importlib import metadata, resources
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.parquet as pq

from parqx.catalog import SourceCatalog
from parqx.data.duckdb import QueryControl
from parqx.data.parquet import ParquetSource
from parqx.query.engine import QueryLimits, QuerySession

EXPECTED_MODULES: tuple[str, ...] = (
    "parqx",
    "parqx.cli",
    "parqx.catalog",
    "parqx.logger",
    "parqx.data",
    "parqx.data.batch",
    "parqx.data.duckdb",
    "parqx.data.parquet",
    "parqx.data.view",
    "parqx.query",
    "parqx.query.engine",
    "parqx.tui.app",
    "parqx.tui.cell_formatter",
    "parqx.tui.screens",
    "parqx.tui.screens.query",
    "parqx.tui.widgets",
    "parqx.tui.widgets.arrow_table",
    "parqx.tui.widgets.result_pane",
    "parqx.tui.widgets.source_pane",
    "parqx.tui.widgets.table_pane",
)


def check_imports() -> None:
    """Every public submodule should import without error."""
    for name in EXPECTED_MODULES:
        __import__(name)
    print(f"OK: imported {len(EXPECTED_MODULES)} modules")


def check_version_metadata() -> None:
    """Installed metadata should match the release being built."""
    project_file = Path(__file__).resolve().parents[2] / "pyproject.toml"
    expected = tomllib.loads(project_file.read_text(encoding="utf-8"))["project"][
        "version"
    ]
    version = metadata.version("parqx")
    assert version == expected, f"installed {version!r}, expected {expected!r}"
    print(f"OK: parqx version metadata = {version!r}")


def check_py_typed_marker() -> None:
    """The `py.typed` marker must be bundled inside the installed package.

    It lives at `src/parqx/py.typed` in the source tree, but only ends up in
    the wheel if the build backend actually picks it up. Easy to silently lose.
    """
    marker = resources.files("parqx") / "py.typed"
    assert marker.is_file(), f"missing py.typed marker: {marker}"
    print("OK: py.typed marker is bundled")


def check_cli_entry_point() -> None:
    """The console script declared in `[project.scripts]` must launch.

    Calling `python -m parqx` would mask a broken entry-point declaration,
    so invoke the installed `parqx` executable directly.
    """
    result = subprocess.run(
        ["parqx", "--version"], capture_output=True, text=True, check=True
    )
    out = result.stdout.strip()
    assert out == f"parqx {metadata.version('parqx')}", (
        f"unexpected CLI output: {out!r}"
    )
    print(f"OK: CLI entry point: {out}")


def check_parquet_browsing() -> None:
    """Read bounded windows through the installed DuckDB-backed source."""
    with TemporaryDirectory(prefix="parqx-browse-smoke-") as temporary:
        directory = Path(temporary) / "year=2026"
        directory.mkdir()
        path = directory / "browse.parquet"
        table = pa.table(
            {"id": range(6_000), "label": [f"row-{index}" for index in range(6_000)]}
        )
        pq.write_table(table, path, row_group_size=1_000)

        source = ParquetSource(path)
        assert source.row_count == table.num_rows
        assert source.schema.names == table.column_names
        first = source.read_window(0, table.num_rows, QueryControl())
        assert first.start == 0
        assert first.table.num_rows == 4_096
        assert first.table.nbytes <= 4 * 1024 * 1024
        assert first.table.equals(table.slice(0, 4_096))
        later = source.read_window(4_200, 4_300, QueryControl())
        assert later.start == 4_200
        assert later.table.equals(table.slice(4_200, 100))
    print("OK: DuckDB browsing preserves row order and bounded windows")


def check_multi_source_query() -> None:
    """Exercise installed Arrow and DuckDB integration with two complete files."""
    with TemporaryDirectory(prefix="parqx-smoke-") as temporary:
        users = Path(temporary) / "users.parquet"
        orders = Path(temporary) / "orders.parquet"
        pq.write_table(pa.table({"id": [1, 2]}), users)
        pq.write_table(pa.table({"user_id": [1, 1, 2], "amount": [10, 15, 7]}), orders)
        catalog = SourceCatalog([users, orders, users])
        assert len(catalog.entries) == 2
        for entry in catalog.entries:
            catalog.mark_ready(entry.spec.source_id)
        with QuerySession(
            catalog.snapshot(),
            'SELECT count(*), sum(o.amount) FROM "users" u '
            'JOIN "orders" o ON u.id = o.user_id',
            QueryControl(),
            QueryLimits(preview_rows=1),
        ) as session:
            preview = session.preview()
        assert preview.table.column(0)[0].as_py() == 3
        assert preview.table.column(1)[0].as_py() == 32
        assert not preview.truncated
    print("OK: multi-source JOIN reads complete inputs with a bounded preview")


def main() -> None:
    check_imports()
    check_version_metadata()
    check_py_typed_marker()
    check_cli_entry_point()
    check_parquet_browsing()
    check_multi_source_query()
    print("All smoke checks passed.")


if __name__ == "__main__":
    main()
