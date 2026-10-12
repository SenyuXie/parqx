from dataclasses import FrozenInstanceError

import pytest

from parqx.config import ParqxConfig


def test_config_defaults_are_immutable_and_keyword_only() -> None:
    config = ParqxConfig()
    assert config.memory_limit == "256MB"
    assert config.threads == 2
    assert config.preview_rows == 10_000
    assert config.preview_bytes == 32 * 1024 * 1024
    assert config.query_batch_rows == 1024
    assert config.page_rows == 4096
    assert config.page_bytes == 4 * 1024 * 1024
    assert config.page_cache_bytes == 32 * 1024 * 1024
    assert config.source_workers == 4
    assert config.page_debounce_seconds == 0.1
    with pytest.raises(FrozenInstanceError):
        config.threads = 3  # type: ignore[misc]  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(TypeError):
        ParqxConfig("1MB")  # type: ignore[misc]  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize("value", [0, -1, 1.5, True])
@pytest.mark.parametrize(
    "field",
    [
        "threads",
        "preview_rows",
        "preview_bytes",
        "query_batch_rows",
        "page_rows",
        "page_bytes",
        "page_cache_bytes",
        "source_workers",
    ],
)
def test_positive_integer_budgets(field: str, value: int | float) -> None:
    with pytest.raises(ValueError, match=field):
        ParqxConfig(**{field: value})  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("value", [-0.1, float("nan"), float("inf")])
def test_finite_nonnegative_debounce(value: float) -> None:
    with pytest.raises(ValueError, match="page_debounce_seconds"):
        ParqxConfig(page_debounce_seconds=value)
    assert ParqxConfig(page_debounce_seconds=0).page_debounce_seconds == 0


def test_memory_syntax_is_left_to_duckdb() -> None:
    with pytest.raises(ValueError, match="memory_limit"):
        ParqxConfig(memory_limit=" ")
    assert ParqxConfig(memory_limit="engine-specific").memory_limit == "engine-specific"
