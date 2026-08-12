from dataclasses import dataclass


@dataclass
class CacheConfig:
    """Cache settings for downloaded audio files.

    Attributes:
        cache_dir: Directory for cached audio files.
        max_cache_age_hours: Delete cached files older than this.
        resume_max_age_days: Delete resume history entries older than this.
        log_max_age_days: Delete session log files older than this.
        log_max_count: Keep at most this many session log files, deleting
            the oldest first once exceeded.
    """
    cache_dir: str = "data"
    max_cache_age_hours: float = 7.0*24
    resume_max_age_days: float = 30.0
    log_max_age_days: float = 14.0
    log_max_count: int = 20


CONFIG = CacheConfig()
