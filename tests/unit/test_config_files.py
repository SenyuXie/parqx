"""Session configuration resolved at CLI startup."""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from parqx.cli import app
from parqx.config import ParqxConfig, TableDefaults


@pytest.fixture
def user_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "project"
    work.mkdir()
    monkeypatch.chdir(work)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    path = home / ".parqx" / "config.toml"
    path.parent.mkdir(parents=True)
    return path


def session_config(*args: str) -> ParqxConfig:
    with patch("parqx.cli.Parqx") as app_class, patch("parqx.cli.setup_logging"):
        app_class.return_value.return_code = 0
        app_class.return_value.load_errors = ()
        result = CliRunner().invoke(app, [*args, "data.parquet"])
    assert result.exit_code == 0, result.output or str(result.exception)
    config = app_class.call_args.kwargs["config"]
    assert isinstance(config, ParqxConfig)
    return config


def test_user_project_and_cli_merge_individual_settings(user_file: Path) -> None:
    user_file.write_text(
        "[query]\npreview_rows = 10\npreview_bytes = 100\n"
        '[duckdb]\nmemory_limit = "64MB"\nthreads = 4\n'
        '[table]\nmax_column_content_width = 22\ncursor_type = "row"\n'
        "zebra_stripes = true\nshow_row_index = false\nshow_header = true\n",
        encoding="utf-8",
    )
    Path("parqx.toml").write_text(
        "[query]\npreview_rows = 20\npreview_bytes = 200\n"
        "[table]\nshow_header = false\n",
        encoding="utf-8",
    )
    assert session_config("--preview-rows", "30") == ParqxConfig(
        preview_rows=30,
        preview_bytes=200,
        threads=4,
        memory_limit="64MB",
        table=TableDefaults(
            max_column_content_width=22,
            cursor_type="row",
            zebra_stripes=True,
            show_row_index=False,
            show_header=False,
        ),
    )


def startup_error(*args: str) -> str:
    with patch("parqx.cli.Parqx") as app_class:
        result = CliRunner().invoke(app, [*args, "data.parquet"])
    assert result.exit_code == 2, result.output or str(result.exception)
    app_class.assert_not_called()
    return result.stderr


@pytest.mark.parametrize(
    ("toml", "invalid_key"),
    [
        ("[typo]", "typo"),
        ("[query]\nthreads = 4", "query.threads"),
        ("[query]\npage_rows = 1", "query.page_rows"),
        ("[table.nested]", "table.nested"),
        ("query = 4", "query"),
        ("[query]\npreview_bytes = 0", "query.preview_bytes"),
        ("[duckdb]\nthreads = true", "duckdb.threads"),
        ("[duckdb]\nthreads = 9223372036854775808", "duckdb.threads"),
        ("[duckdb]\nmemory_limit = 128", "duckdb.memory_limit"),
        ("[table]\nmax_column_content_width = 1", "table.max_column_content_width"),
        ('[table]\ncursor_type = "block"', "table.cursor_type"),
        ("[table]\nshow_header = 1", "table.show_header"),
    ],
)
def test_invalid_user_file_reports_its_path_and_key_before_overrides(
    user_file: Path, toml: str, invalid_key: str
) -> None:
    user_file.write_text(toml, encoding="utf-8")
    error = startup_error("--preview-bytes", "100", "--memory-limit", "64MB")
    assert str(user_file) in error
    assert invalid_key in error


@pytest.mark.parametrize("cli_override", [False, True])
def test_invalid_duckdb_memory_is_a_startup_error(
    user_file: Path, cli_override: bool
) -> None:
    if cli_override:
        error = startup_error("--memory-limit", "40 gallons")
        assert "--memory-limit" in error
    else:
        user_file.write_text('[duckdb]\nmemory_limit = "40 gallons"', encoding="utf-8")
        error = startup_error("--memory-limit", "64MB")
        assert str(user_file) in error
        assert "duckdb.memory_limit" in error


@pytest.mark.parametrize("malformed", [b"[query", b"[query]\n\xff"])
def test_malformed_first_project_file_reports_path_without_fallback(
    user_file: Path, malformed: bytes
) -> None:
    fallback = Path(".parqx/config.toml")
    fallback.parent.mkdir()
    fallback.write_text("", encoding="utf-8")
    Path("parqx.toml").write_bytes(malformed)
    assert "parqx.toml" in startup_error()


def test_project_file_preference_and_explicit_file_isolation(user_file: Path) -> None:
    fallback = Path(".parqx/config.toml")
    fallback.parent.mkdir()
    fallback.write_text("[query]\npreview_rows = 3", encoding="utf-8")
    assert session_config() == ParqxConfig(preview_rows=3)

    Path("parqx.toml").write_text("", encoding="utf-8")
    assert session_config() == ParqxConfig()

    user_file.write_text("[query]\npreview_rows = 4", encoding="utf-8")
    explicit = Path("team.toml")
    explicit.write_text("[query]\npreview_bytes = 5", encoding="utf-8")
    assert session_config("--config", str(explicit)) == ParqxConfig(preview_bytes=5)


def test_missing_explicit_file_reports_its_path(user_file: Path) -> None:
    assert "missing.toml" in startup_error("--config", "missing.toml")


def test_unreadable_project_file_reports_its_path(user_file: Path) -> None:
    Path("parqx.toml").mkdir()
    assert "parqx.toml" in startup_error()
