"""Tests for config.py — CacheConfig, CONFIG singleton.""" 

from config import CacheConfig, CONFIG


class TestCacheConfig:
    def test_defaults(self):
        cfg = CacheConfig()
        assert cfg.cache_dir == "data"
        assert cfg.max_cache_age_hours == 7.0 * 24

    def test_cache_dir_custom(self):
        cfg = CacheConfig(cache_dir="/tmp/cache")
        assert cfg.cache_dir == "/tmp/cache"

    def test_max_age_custom(self):
        cfg = CacheConfig(max_cache_age_hours=1.0)
        assert cfg.max_cache_age_hours == 1.0

    def test_immutable_like(self):
        """Ensure it's a dataclass with expected repr."""
        cfg = CacheConfig()
        assert "CacheConfig(cache_dir=" in repr(cfg)

    def test_negative_age_zero_or_more(self):
        """Negative values should be allowed (caller's responsibility)."""
        cfg = CacheConfig(max_cache_age_hours=-1.0)
        assert cfg.max_cache_age_hours == -1.0


class TestCONFIG:
    def test_concept_is_cacheconfig(self):
        assert isinstance(CONFIG, CacheConfig)

    def test_concept_default_cache_dir(self):
        assert CONFIG.cache_dir == "data"

    def test_concept_max_age_property_exists(self):
        # Just ensure the attribute is accessible
        assert hasattr(CONFIG, "max_cache_age_hours")
