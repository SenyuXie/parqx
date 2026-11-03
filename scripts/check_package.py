"""Build and smoke-test fresh distributions in isolated environments."""

import subprocess
import sys
from pathlib import Path
from shlex import join
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def run(*command: str) -> None:
    """Run a fixed check from the repository root and propagate failures."""
    print(f"+ {join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)  # noqa: S603 - Fixed check commands.


def main() -> None:
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


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
