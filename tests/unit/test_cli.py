"""CLI argument forwarding and exit status; application behavior lives elsewhere."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click import unstyle
from typer.testing import CliRunner

from parqx.cli import app
from parqx.data.catalog import SourceCatalog, SourceIssue

runner = CliRunner()


@pytest.mark.parametrize(
    ("option", "output"), [("--version", "parqx"), ("--help", "PATH")]
)
def test_information_flags_exit_without_starting_app(option: str, output: str) -> None:
    with patch("parqx.cli.ParqxApp") as app_class:
        result = runner.invoke(app, [option])
    assert result.exit_code == 0
    assert output in unstyle(result.stdout)
    app_class.assert_not_called()


def test_paths_are_forwarded_in_order(tmp_path: Path) -> None:
    paths = [tmp_path / "second.parquet", tmp_path / "first.parquet"]
    with patch("parqx.cli.ParqxApp") as app_class:
        app_class.return_value.load_errors = ()
        result = runner.invoke(app, [str(path) for path in paths])
    assert result.exit_code == 0
    app_class.assert_called_once_with(paths=paths)
    app_class.return_value.run.assert_called_once_with()


def test_partial_failure_reports_each_path_after_app_finishes(tmp_path: Path) -> None:
    paths = [
        tmp_path / name for name in ("good.parquet", "missing.parquet", "bad.parquet")
    ]
    catalog = SourceCatalog(paths[1:])
    errors = tuple(SourceIssue(entry.spec, "Cannot read") for entry in catalog.entries)
    with patch("parqx.cli.ParqxApp") as app_class:
        # Errors only become available once the interactive application exits.
        app_class.return_value.load_errors = ()

        def finish() -> None:
            app_class.return_value.load_errors = errors

        app_class.return_value.run.side_effect = finish
        result = runner.invoke(app, [str(path) for path in paths])
    assert result.exit_code == 1
    assert str(paths[1]) in result.stderr
    assert str(paths[2]) in result.stderr
    assert result.stderr.count("Cannot read") == 2
    app_class.return_value.run.assert_called_once_with()


def test_at_least_one_path_is_required() -> None:
    with patch("parqx.cli.ParqxApp") as app_class:
        result = runner.invoke(app, [])
    assert result.exit_code == 2
    app_class.assert_not_called()
