from dataclasses import dataclass


@dataclass
class CacheConfig:
    """Cache settings for downloaded audio files.

    Attributes:
        cache_dir: Directory for cached audio files.
        max_cache_age_hours: Delete cached files older than this.
    """
    cache_dir: str = "data"
    max_cache_age_hours: float = 7.0


CONFIG = CacheConfig()
