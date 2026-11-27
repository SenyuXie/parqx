"""Resolve user-facing settings for a CLI session."""

import tomllib
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from ._model import ParqxConfig

_FILE_KEYS = {
    "query": {"preview_rows", "preview_bytes"},
    "duckdb": {"memory_limit", "threads"},
    "table": {
        "max_column_content_width",
        "cursor_type",
        "zebra_stripes",
        "show_row_index",
        "show_header",
    },
}


def load_config(
    path: Path | None,
    *,
    preview_rows: int | None = None,
    preview_bytes: int | None = None,
    memory_limit: str | None = None,
    threads: int | None = None,
) -> ParqxConfig:
    """Apply user, project and explicit CLI settings in precedence order."""
    config = ParqxConfig()
    if path is not None:
        config = _apply_file(config, path)
    else:
        try:
            home = Path.home()
        except RuntimeError:
            home = None
        if home is not None:
            with suppress(FileNotFoundError, NotADirectoryError):
                config = _apply_file(config, home / ".parqx" / "config.toml")
        for project in (Path("parqx.toml"), Path(".parqx/config.toml")):
            try:
                config = _apply_file(config, project)
            except (FileNotFoundError, NotADirectoryError):
                continue
            break
    if memory_limit is not None:
        _check_memory_limit(memory_limit, "--memory-limit")
    return replace(
        config,
        preview_rows=config.preview_rows if preview_rows is None else preview_rows,
        preview_bytes=config.preview_bytes if preview_bytes is None else preview_bytes,
        memory_limit=config.memory_limit if memory_limit is None else memory_limit,
        threads=config.threads if threads is None else threads,
    )


def _apply_file(config: ParqxConfig, path: Path) -> ParqxConfig:
    with path.open("rb") as stream:
        try:
            sections = tomllib.load(stream)
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
    for section, values in sections.items():
        if section not in _FILE_KEYS:
            raise ValueError(f"{path}: unknown setting {section}")
        if not isinstance(values, dict):
            raise ValueError(f"{path}: {section} must be a TOML table")
        # TOML section keys are strings. The config constructors check leaf values.
        settings = cast(dict[str, Any], values)
        for key in settings:
            if key not in _FILE_KEYS[section]:
                raise ValueError(f"{path}: unknown setting {section}.{key}")
        try:
            if section == "table":
                config = replace(config, table=replace(config.table, **settings))
            else:
                if "memory_limit" in settings:
                    _check_memory_limit(settings["memory_limit"], "memory_limit")
                config = replace(config, **settings)
        except ValueError as exc:
            raise ValueError(f"{path}: {section}.{exc}") from exc
    return config


def _check_memory_limit(value: object, name: str) -> None:
    import duckdb

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    try:
        # Let the same DuckDB parser used by source reads and queries check the unit.
        duckdb.connect(config={"threads": 1, "memory_limit": value}).close()
    except duckdb.Error as exc:
        raise ValueError(f"{name}: {exc}") from exc
