import pytest

from parqx.data.cache import BoundedLRUCache


def test_budget_must_be_positive() -> None:
    with pytest.raises(ValueError, match="budget_bytes must be positive"):
        BoundedLRUCache[int, str](0, len)


def test_set_get_updates_current_bytes() -> None:
    cache = BoundedLRUCache[str, str](budget_bytes=10, sizeof=len)

    cache["a"] = "123"

    assert cache["a"] == "123"
    assert cache.current_bytes == 3


def test_evicts_oldest_when_budget_exceeded() -> None:
    cache = BoundedLRUCache[str, str](budget_bytes=5, sizeof=len)

    cache["a"] = "123"
    cache["b"] = "456"

    assert "a" not in cache
    assert "b" in cache
    assert cache.current_bytes == 3


def test_get_marks_entry_recently_used() -> None:
    cache = BoundedLRUCache[str, str](budget_bytes=5, sizeof=len)
    cache["a"] = "12"
    cache["b"] = "34"

    _ = cache["a"]
    cache["c"] = "56"

    assert "a" in cache
    assert "b" not in cache
    assert "c" in cache


def test_replace_same_key_updates_size() -> None:
    cache = BoundedLRUCache[str, str](budget_bytes=10, sizeof=len)

    cache["a"] = "12"
    cache["a"] = "12345"

    assert cache.current_bytes == 5
    assert cache["a"] == "12345"


def test_oversized_entry_is_kept_when_alone() -> None:
    cache = BoundedLRUCache[str, str](budget_bytes=3, sizeof=len)

    cache["a"] = "12345"

    assert len(cache) == 1
    assert cache["a"] == "12345"
    assert cache.current_bytes == 5


def test_clear_resets_cache() -> None:
    cache = BoundedLRUCache[str, str](budget_bytes=10, sizeof=len)
    cache["a"] = "123"

    cache.clear()

    assert len(cache) == 0
    assert cache.current_bytes == 0
