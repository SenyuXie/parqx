"""CLI-level smoke tests via Typer's test runner."""

from pathlib import Path
from unittest.mock import patch

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


def test_query_option_is_passed_to_app(small_parquet: Path) -> None:
    with patch("parqx.cli.ParqxApp") as app_class:
        app_class.return_value.load_error = None
        result = runner.invoke(app, [str(small_parquet), "--query", "SELECT 42"])
    assert result.exit_code == 0
    app_class.assert_called_once_with(path=small_parquet, initial_sql="SELECT 42")
