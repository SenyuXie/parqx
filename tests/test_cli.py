"""CLI-level smoke tests via Typer's test runner."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click import unstyle
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
    output = unstyle(result.stdout)
    assert "PATH" in output
    assert "--query" not in output
    assert "-q" not in output


def test_nonexistent_path_exits_nonzero(tmp_path: Path) -> None:
    missing = tmp_path / "no.parquet"
    result = runner.invoke(app, [str(missing)])
    assert result.exit_code != 0


def test_path_is_forwarded_to_app(small_parquet: Path) -> None:
    with patch("parqx.cli.ParqxApp") as app_class:
        app_class.return_value.load_error = None
        result = runner.invoke(app, [str(small_parquet)])

    assert result.exit_code == 0
    app_class.assert_called_once_with(path=small_parquet)
    app_class.return_value.run.assert_called_once_with()


@pytest.mark.parametrize("option", ["--query", "-q"])
def test_query_option_is_rejected(small_parquet: Path, option: str) -> None:
    with patch("parqx.cli.ParqxApp") as app_class:
        result = runner.invoke(app, [str(small_parquet), option, "SELECT * FROM smoke"])

    assert result.exit_code == 2
    assert "No such option" in unstyle(result.output)
    app_class.assert_not_called()
