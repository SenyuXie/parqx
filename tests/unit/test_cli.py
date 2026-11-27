"""CLI argument forwarding and exit status; application behavior lives elsewhere."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click import unstyle
from typer.testing import CliRunner

from parqx.catalog import SourceCatalog, SourceIssue
from parqx.cli import app
from parqx.config import ParqxConfig

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "user"))


@pytest.mark.parametrize(
    ("option", "output"), [("--version", "parqx"), ("--help", "PATH")]
)
def test_information_flags_exit_without_starting_app(option: str, output: str) -> None:
    Path("parqx.toml").write_text("invalid TOML", encoding="utf-8")
    with patch("parqx.cli.Parqx") as app_class:
        result = runner.invoke(app, [option])
    assert result.exit_code == 0
    assert output in unstyle(result.stdout)
    app_class.assert_not_called()


def test_paths_are_forwarded_in_order(tmp_path: Path) -> None:
    paths = [tmp_path / "second.parquet", tmp_path / "first.parquet"]
    with patch("parqx.cli.Parqx") as app_class, patch("parqx.cli.setup_logging"):
        app_class.return_value.load_errors = ()
        app_class.return_value.return_code = 0
        result = runner.invoke(app, [str(path) for path in paths])
    assert result.exit_code == 0
    app_class.assert_called_once_with(paths=paths, config=ParqxConfig())
    app_class.return_value.run.assert_called_once_with()


def test_explicit_budgets_reach_app(tmp_path: Path) -> None:
    path = tmp_path / "source.parquet"
    with patch("parqx.cli.Parqx") as app_class, patch("parqx.cli.setup_logging"):
        app_class.return_value.return_code = 0
        app_class.return_value.load_errors = ()
        result = runner.invoke(
            app,
            [
                "--memory-limit",
                "64MB",
                "--threads",
                "1",
                "--preview-bytes",
                "200",
                str(path),
            ],
        )
    assert result.exit_code == 0, result.output
    app_class.assert_called_once_with(
        paths=[path],
        config=ParqxConfig(memory_limit="64MB", threads=1, preview_bytes=200),
    )


def test_bad_config_exits_before_starting_app(tmp_path: Path) -> None:
    path = tmp_path / "broken.toml"
    path.write_text("[query]\npreview_rows = false\n", encoding="utf-8")
    with patch("parqx.cli.Parqx") as app_class:
        result = runner.invoke(app, ["--config", str(path), "source.parquet"])
    assert result.exit_code == 2
    assert str(path) in result.stderr
    assert "query.preview_rows" in result.stderr
    app_class.assert_not_called()


@pytest.mark.parametrize("return_code", [0, 1, 7])
def test_application_exit_code_is_preserved_without_load_errors(
    tmp_path: Path, return_code: int
) -> None:
    with patch("parqx.cli.Parqx") as app_class, patch("parqx.cli.setup_logging"):
        app_class.return_value.load_errors = ()
        app_class.return_value.return_code = None

        def finish() -> None:
            app_class.return_value.return_code = return_code

        app_class.return_value.run.side_effect = finish
        result = runner.invoke(app, [str(tmp_path / "source.parquet")])
    assert result.exit_code == return_code
    assert result.stderr == ""
    app_class.return_value.run.assert_called_once_with()


@pytest.mark.parametrize(("return_code", "expected_code"), [(0, 1), (1, 1), (7, 7)])
def test_partial_failure_reports_each_path_after_app_finishes(
    tmp_path: Path, return_code: int, expected_code: int
) -> None:
    paths = [
        tmp_path / name for name in ("good.parquet", "missing.parquet", "bad.parquet")
    ]
    catalog = SourceCatalog(paths[1:])
    errors = tuple(SourceIssue(entry.spec, "Cannot read") for entry in catalog.entries)
    with patch("parqx.cli.Parqx") as app_class, patch("parqx.cli.setup_logging"):
        # Errors only become available once the interactive application exits.
        app_class.return_value.load_errors = ()
        app_class.return_value.return_code = None

        def finish() -> None:
            app_class.return_value.load_errors = errors
            app_class.return_value.return_code = return_code

        app_class.return_value.run.side_effect = finish
        result = runner.invoke(app, [str(path) for path in paths])
    assert result.exit_code == expected_code
    assert str(paths[1]) in result.stderr
    assert str(paths[2]) in result.stderr
    assert result.stderr.count("Cannot read") == 2
    app_class.return_value.run.assert_called_once_with()


def test_at_least_one_path_is_required() -> None:
    with patch("parqx.cli.Parqx") as app_class:
        result = runner.invoke(app, [])
    assert result.exit_code == 2
    app_class.assert_not_called()
