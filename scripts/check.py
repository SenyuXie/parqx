"""Run the same validation commands locally and in CI."""

import argparse
import subprocess
import sys
from pathlib import Path
from shlex import join
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
CHECKS: dict[str, tuple[tuple[str, ...], ...]] = {
    "lint": (("ruff", "check"), ("ruff", "format", "--check")),
    "typecheck": (("mypy", "src", "tests", "scripts"), ("pyright",)),
    "full": (("pytest", "-q", "tests"),),
    "core": (("pytest", "-q", "tests/unit", "tests/smoke"),),
}


def run(*command: str) -> None:
    """Run a fixed check from the repository root and propagate failures."""
    print(f"+ {join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)  # noqa: S603 - Fixed check commands.


def check_package() -> None:
    """Build fresh artifacts and smoke test their isolated installations."""
    with TemporaryDirectory(prefix="parqx-check-dist-") as directory:
        run("uv", "build", "--out-dir", directory)
        for pattern in ("*.whl", "*.tar.gz"):
            (artifact,) = Path(directory).glob(pattern)
            run(
                "uv",
                "run",
                "--isolated",
                "--no-project",
                "--with",
                str(artifact),
                "tests/distribution/check_install.py",
            )


def main() -> int:
    """Run all checks by default, or a named CI job's checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "check",
        nargs="?",
        default="all",
        choices=("all", *CHECKS, "package"),
        help="all checks (default), lint, typecheck, full/core tests, or package",
    )
    args = parser.parse_args()
    checks = (
        ("lint", "typecheck", "full", "package")
        if args.check == "all"
        else (args.check,)
    )
    try:
        for check in checks:
            if check == "package":
                check_package()
            else:
                for command in CHECKS[check]:
                    run("uv", "run", "--locked", *command)
    except subprocess.CalledProcessError as error:
        return error.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
