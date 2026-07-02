"""The bounded LRU cache."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable


class BoundedLRUCache[K, V]:
    """LRU cache bounded by a byte budget."""

    def __init__(self, budget_bytes: int, sizeof: Callable[[V], int]) -> None:
        """Initialize the cache.

        Args:
            budget_bytes: Maximum cached size in bytes.
            sizeof: Function returning the byte size of a cached value.

        Raises:
            ValueError: If budget_bytes is not positive.
        """
        if budget_bytes <= 0:
            raise ValueError(f"budget_bytes must be positive, got {budget_bytes}")
        self._budget_bytes = budget_bytes
        self._sizeof = sizeof
        self._data: OrderedDict[K, V] = OrderedDict()
        self._sizes: dict[K, int] = {}
        self._current_bytes = 0

    def __contains__(self, key: K) -> bool:
        """Return whether key is present."""
        return key in self._data

    def __getitem__(self, key: K) -> V:
        """Return cached value and mark it as recently used."""
        value = self._data[key]
        self._data.move_to_end(key)
        return value

    def __setitem__(self, key: K, value: V) -> None:
        """Insert or replace a cached value."""
        if key in self._data:
            self._current_bytes -= self._sizes.pop(key)
            del self._data[key]

        size = self._sizeof(value)
        self._data[key] = value
        self._sizes[key] = size
        self._current_bytes += size
        self._evict()

    def __delitem__(self, key: K) -> None:
        """Remove a cached value."""
        self._current_bytes -= self._sizes.pop(key)
        del self._data[key]

    def __len__(self) -> int:
        """Return number of cached entries."""
        return len(self._data)

    @property
    def current_bytes(self) -> int:
        """Current cached byte size."""
        return self._current_bytes

    @property
    def budget_bytes(self) -> int:
        """Configured byte budget."""
        return self._budget_bytes

    def clear(self) -> None:
        """Clear all cached values."""
        self._data.clear()
        self._sizes.clear()
        self._current_bytes = 0

    def _evict(self) -> None:
        while self._current_bytes > self._budget_bytes and len(self._data) > 1:
            oldest_key, _ = self._data.popitem(last=False)
            self._current_bytes -= self._sizes.pop(oldest_key)
