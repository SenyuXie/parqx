"""The Parqx runtime configuration."""

from dataclasses import dataclass

DEFAULT_MAX_CACHE_BYTES = 256 * 1024 * 1024  # 256MiB


@dataclass
class ParqxConfig:
    """Runtime configuration for Parqx."""

    max_cache_bytes: int = DEFAULT_MAX_CACHE_BYTES
    """TODO."""

    def __post_init__(self) -> None:
        """TODO."""
        if self.max_cache_bytes <= 0:
            raise ValueError
