"""Smoke test for a built parqx distribution.

Run against an installed wheel or sdist (NOT the source tree) to catch
packaging-time regressions that the pytest suite cannot see: a missing
submodule, a broken `[project.scripts]` entry point, an unshipped
`py.typed` marker, or a runtime dependency that was only available in dev.

Invoked from `.github/workflows/release.yml`:

    uv run --isolated --no-project --with dist/*.whl tests/smoke_test.py
    uv run --isolated --no-project --with dist/*.tar.gz tests/smoke_test.py

The `--isolated --no-project` flags mean the only things available are the
standard library, the built parqx artifact, and parqx's declared runtime
dependencies. Do not import pytest or any other dev-only package here.
"""

from __future__ import annotations

import subprocess
from importlib import metadata, resources

EXPECTED_MODULES: tuple[str, ...] = (
    "parqx",
    "parqx.cli",
    "parqx.logger",
    "parqx.tui.app",
    "parqx.tui.widgets",
    "parqx.tui.widgets.arrow_table",
    "parqx.tui.widgets.file_loading",
)


def check_imports() -> None:
    """Every public submodule should import without error."""
    for name in EXPECTED_MODULES:
        __import__(name)
    print(f"OK: imported {len(EXPECTED_MODULES)} modules")


def check_version_metadata() -> None:
    """`importlib.metadata` should resolve a non-empty version string."""
    version = metadata.version("parqx")
    assert version, "parqx version metadata is empty"
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
    assert "parqx" in out, f"unexpected CLI output: {out!r}"
    print(f"OK: CLI entry point: {out}")


def main() -> None:
    check_imports()
    check_version_metadata()
    check_py_typed_marker()
    check_cli_entry_point()
    print("All smoke checks passed.")


if __name__ == "__main__":
    main()
