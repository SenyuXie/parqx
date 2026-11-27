"""Session configuration resolved at CLI startup."""

import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from parqx.config import ParqxConfig, TableDefaults
from parqx.config_file import load_config


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
    assert load_config(None, preview_rows=30) == ParqxConfig(
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
        ('[duckdb]\nmemory_limit = "40 gallons"', "duckdb.memory_limit"),
        ("[table]\nmax_column_content_width = 1", "table.max_column_content_width"),
        ('[table]\ncursor_type = "block"', "table.cursor_type"),
        ("[table]\nshow_header = 1", "table.show_header"),
    ],
)
def test_invalid_user_file_reports_its_path_and_key_before_overrides(
    user_file: Path, toml: str, invalid_key: str
) -> None:
    user_file.write_text(toml, encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape(invalid_key)) as error:
        load_config(None, preview_bytes=100, memory_limit="64MB")
    assert str(user_file) in str(error.value)


def test_invalid_memory_override_reports_cli_option(user_file: Path) -> None:
    with pytest.raises(ValueError, match="--memory-limit"):
        load_config(None, memory_limit="40 gallons")


@pytest.mark.parametrize("malformed", [b"[query", b"[query]\n\xff"])
def test_malformed_first_project_file_reports_path_without_fallback(
    user_file: Path, malformed: bytes
) -> None:
    fallback = Path(".parqx/config.toml")
    fallback.parent.mkdir()
    fallback.write_text("", encoding="utf-8")
    Path("parqx.toml").write_bytes(malformed)
    with pytest.raises(ValueError, match=r"parqx\.toml"):
        load_config(None)


def test_toml_integer_parse_failure_reports_file_path(tmp_path: Path) -> None:
    digit_limit = sys.get_int_max_str_digits()
    if not digit_limit:
        pytest.skip("Python's integer string conversion limit is disabled")
    path = tmp_path / "oversized.toml"
    path.write_text(
        "[query]\npreview_rows = " + "1" * (digit_limit + 1), encoding="utf-8"
    )
    with pytest.raises(ValueError, match=re.escape(str(path))):
        load_config(path)


def test_project_file_preference_and_explicit_file_isolation(user_file: Path) -> None:
    fallback = Path(".parqx/config.toml")
    fallback.parent.mkdir()
    fallback.write_text("[query]\npreview_rows = 3", encoding="utf-8")
    assert load_config(None) == ParqxConfig(preview_rows=3)

    Path("parqx.toml").write_text("", encoding="utf-8")
    assert load_config(None) == ParqxConfig()

    user_file.write_text("[query]\npreview_rows = 4", encoding="utf-8")
    explicit = Path("team.toml")
    explicit.write_text("[query]\npreview_bytes = 5", encoding="utf-8")
    assert load_config(explicit) == ParqxConfig(preview_bytes=5)


def test_missing_explicit_file_reports_its_path(user_file: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"missing\.toml"):
        load_config(Path("missing.toml"))


def test_unreadable_project_file_reports_its_path(user_file: Path) -> None:
    Path("parqx.toml").mkdir()
    with pytest.raises(OSError, match=r"parqx\.toml"):
        load_config(None)


def test_non_directory_parents_mean_discovered_files_are_absent(
    user_file: Path,
) -> None:
    user_file.parent.rmdir()
    user_file.parent.touch()
    Path(".parqx").touch()
    assert load_config(None) == ParqxConfig()
    with pytest.raises(OSError, match=r"config\.toml"):
        load_config(user_file)


def test_project_config_is_used_when_home_cannot_be_resolved(user_file: Path) -> None:
    Path("parqx.toml").write_text("[query]\npreview_rows = 42", encoding="utf-8")
    with patch.object(Path, "home", side_effect=RuntimeError("No home directory")):
        assert load_config(None) == ParqxConfig(preview_rows=42)
