"""CLI-level smoke tests via Typer's test runner."""

from pathlib import Path

from typer.testing import CliRunner

from parqx.cli import app

runner = CliRunner()


def test_version_flag_prints_version_and_exits() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "parqx" in result.stdout


def test_help_lists_path_argument() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "PATH" in result.stdout


def test_nonexistent_path_exits_nonzero(tmp_path: Path) -> None:
    missing = tmp_path / "no.parquet"
    result = runner.invoke(app, [str(missing)])
    assert result.exit_code != 0
