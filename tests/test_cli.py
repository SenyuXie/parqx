"""CLI-level smoke tests via Typer's test runner."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click import unstyle
from typer.testing import CliRunner

from parqx.cli import app
from parqx.data.catalog import SourceCatalog, SourceIssue

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
    source = SourceCatalog([missing]).entries[0].spec
    with patch("parqx.cli.ParqxApp") as app_class:
        app_class.return_value.load_errors = (SourceIssue(source, "File not found"),)
        result = runner.invoke(app, [str(missing)])
    assert result.exit_code == 1
    assert str(missing) in result.stderr
    assert "File not found" in result.stderr


def test_path_is_forwarded_to_app(small_parquet: Path) -> None:
    with patch("parqx.cli.ParqxApp") as app_class:
        app_class.return_value.load_errors = ()
        result = runner.invoke(app, [str(small_parquet)])

    assert result.exit_code == 0
    app_class.assert_called_once_with(paths=[small_parquet])
    app_class.return_value.run.assert_called_once_with()


def test_multiple_paths_are_forwarded_in_order(small_parquet: Path) -> None:
    second = small_parquet.with_name("second.parquet")
    with patch("parqx.cli.ParqxApp") as app_class:
        app_class.return_value.load_errors = ()
        result = runner.invoke(app, [str(second), str(small_parquet)])
    assert result.exit_code == 0
    app_class.assert_called_once_with(paths=[second, small_parquet])


def test_partial_failure_reports_each_path_after_app_finishes(
    small_parquet: Path,
) -> None:
    missing = small_parquet.with_name("missing.parquet")
    bad = small_parquet.with_name("bad.parquet")
    catalog = SourceCatalog([missing, bad])
    errors = tuple(SourceIssue(entry.spec, "Cannot read") for entry in catalog.entries)
    with patch("parqx.cli.ParqxApp") as app_class:
        app_class.return_value.load_errors = errors
        result = runner.invoke(app, [str(small_parquet), str(missing), str(bad)])
    assert result.exit_code == 1
    assert str(missing) in result.stderr
    assert str(bad) in result.stderr
    app_class.return_value.run.assert_called_once_with()


def test_at_least_one_path_is_required() -> None:
    with patch("parqx.cli.ParqxApp") as app_class:
        result = runner.invoke(app, [])
    assert result.exit_code == 2
    app_class.assert_not_called()


@pytest.mark.parametrize("option", ["--query", "-q"])
def test_query_option_is_rejected(small_parquet: Path, option: str) -> None:
    with patch("parqx.cli.ParqxApp") as app_class:
        result = runner.invoke(app, [str(small_parquet), option, "SELECT * FROM smoke"])

    assert result.exit_code == 2
    assert "No such option" in unstyle(result.output)
    app_class.assert_not_called()
