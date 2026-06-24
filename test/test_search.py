"""Tests for search.py — video ID extraction, SearchResult, cache functions.""" 

import os
import time

import pytest

from search import (
    _extract_video_id,
    _marker_path,
    _check_cache,
    _cleanup_cache,
    _remove_partial,
    SearchResult,
)
from config import CONFIG


# ---------------------------------------------------------------------------
# _extract_video_id
# ---------------------------------------------------------------------------

class TestExtractVideoId:
    def test_standard_url(self):
        assert _extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short_url(self):
        assert _extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_url_with_extra_params(self):
        assert _extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s") == "dQw4w9WgXcQ"

    def test_url_with_list_param_before(self):
        assert _extract_video_id("https://www.youtube.com/watch?list=PL&v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_embed_url(self):
        assert _extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") is None

    def test_invalid_url(self):
        assert _extract_video_id("https://example.com") is None

    def test_empty_string(self):
        assert _extract_video_id("") is None

    def test_none_input_raises(self):
        with pytest.raises((AttributeError, TypeError)):
            _extract_video_id(None)  # type: ignore

    def test_short_id_11_chars_exact(self):
        assert _extract_video_id("https://youtu.be/abcdefghijk") == "abcdefghijk"

    def test_short_id_less_than_11_chars(self):
        assert _extract_video_id("https://youtu.be/short") is None


# ---------------------------------------------------------------------------
# SearchResult.from_dict
# ---------------------------------------------------------------------------

class TestSearchResultFromDict:
    def test_full_data(self, sample_search_data):
        res = SearchResult.from_dict(sample_search_data)
        assert res.id == "dQw4w9WgXcQ"
        assert res.title == "Rick Astley - Never Gonna Give You Up"
        assert res.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert res.uploader == "Rick Astley"

    def test_duration_minutes(self):
        data = {
            "id": "abc",
            "title": "Test",
            "webpage_url": "https://youtu.be/abc",
            "duration": 125,
            "uploader": "Tester",
        }
        res = SearchResult.from_dict(data)
        assert res.duration_str == "2:05"

    def test_duration_hours(self):
        data = {
            "id": "abc",
            "title": "Long Video",
            "webpage_url": "https://youtu.be/abc",
            "duration": 3661,
            "uploader": "Tester",
        }
        res = SearchResult.from_dict(data)
        assert res.duration_str == "1:01:01"

    def test_zero_duration(self):
        data = {
            "id": "abc",
            "title": "Zero",
            "webpage_url": "https://youtu.be/abc",
            "duration": 0,
            "uploader": "Tester",
        }
        res = SearchResult.from_dict(data)
        assert res.duration_str == "0:00"

    def test_missing_fields_defaults(self):
        res = SearchResult.from_dict({})
        assert res.title == "Unknown Title"
        assert res.uploader == "Unknown Uploader"
        assert res.duration_str == "0:00"


# ---------------------------------------------------------------------------
# _marker_path
# ---------------------------------------------------------------------------

class TestMarkerPath:
    def test_returns_string(self, sample_video_id):
        path = _marker_path(sample_video_id)
        assert isinstance(path, str)
        assert path.endswith(".done")
        assert sample_video_id in path

    def test_uses_data_dir(self, sample_video_id):
        path = _marker_path(sample_video_id)
        # Should contain the DOWNLOAD_DIR ("data")
        assert "data" in path or "data" in os.path.normpath(path)


# ---------------------------------------------------------------------------
# _check_cache
# ---------------------------------------------------------------------------

class TestCheckCache:
    def test_marker_without_file(self, tmp_path, monkeypatch, sample_video_id):
        monkeypatch.setattr("search.DOWNLOAD_DIR", str(tmp_path))
        # Create marker but no audio file
        marker = _marker_path(sample_video_id)
        marker_path = os.path.join(str(tmp_path), os.path.basename(marker))
        with open(marker_path, "w") as f:
            f.write("/tmp/dummy")
        assert _check_cache(sample_video_id) is None
        # Stale marker should also be deleted
        assert not os.path.exists(marker_path)

    def test_marker_with_file(self, tmp_path, monkeypatch, sample_video_id):
        monkeypatch.setattr("search.DOWNLOAD_DIR", str(tmp_path))
        # Create audio file
        audio = os.path.join(str(tmp_path), f"ytplay-{sample_video_id}.webm")
        with open(audio, "w") as f:
            f.write("fake audio")
        # Create marker
        marker = os.path.join(str(tmp_path), f"ytplay-{sample_video_id}.done")
        with open(marker, "w") as f:
            f.write("cached")
        assert _check_cache(sample_video_id) == audio

    def test_no_marker(self, tmp_path, monkeypatch, sample_video_id):
        monkeypatch.setattr("search.DOWNLOAD_DIR", str(tmp_path))
        assert _check_cache(sample_video_id) is None


# ---------------------------------------------------------------------------
# _remove_partial
# ---------------------------------------------------------------------------

class TestRemovePartial:
    def test_removes_partial_files(self, tmp_path, monkeypatch, sample_video_id):
        monkeypatch.setattr("search.DOWNLOAD_DIR", str(tmp_path))
        # Create partial files
        for ext in [".webm.part", ".m4a", ".webm"]:
            path = os.path.join(str(tmp_path), f"ytplay-{sample_video_id}{ext}")
            with open(path, "w") as f:
                f.write("partial")
        _remove_partial(sample_video_id)
        remaining = [f for f in os.listdir(str(tmp_path)) if sample_video_id in f]
        assert len(remaining) == 0

    def test_keeps_done_marker(self, tmp_path, monkeypatch, sample_video_id):
        monkeypatch.setattr("search.DOWNLOAD_DIR", str(tmp_path))
        # Create done marker
        marker = os.path.join(str(tmp_path), f"ytplay-{sample_video_id}.done")
        with open(marker, "w") as f:
            f.write("done")
        _remove_partial(sample_video_id)
        assert os.path.exists(marker)

    def test_keeps_unrelated_files(self, tmp_path, monkeypatch, sample_video_id):
        monkeypatch.setattr("search.DOWNLOAD_DIR", str(tmp_path))
        other = os.path.join(str(tmp_path), "other-file.txt")
        with open(other, "w") as f:
            f.write("keep")
        _remove_partial(sample_video_id)
        assert os.path.exists(other)


# ---------------------------------------------------------------------------
# _cleanup_cache
# ---------------------------------------------------------------------------

class TestCleanupCache:
    def test_removes_old_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("search.DOWNLOAD_DIR", str(tmp_path))
        old_file = os.path.join(str(tmp_path), "ytplay-old.webm")
        with open(old_file, "w") as f:
            f.write("old")
        # Set mtime far in the past
        old_mtime = time.time() - (CONFIG.max_cache_age_hours * 3600 + 3600)
        os.utime(old_file, (old_mtime, old_mtime))
        _cleanup_cache()
        assert not os.path.exists(old_file)

    def test_keeps_recent_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("search.DOWNLOAD_DIR", str(tmp_path))
        new_file = os.path.join(str(tmp_path), "ytplay-new.webm")
        with open(new_file, "w") as f:
            f.write("new")
        _cleanup_cache()
        assert os.path.exists(new_file)

    def test_ignores_non_ytplay_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("search.DOWNLOAD_DIR", str(tmp_path))
        other = os.path.join(str(tmp_path), "user-data.txt")
        with open(other, "w") as f:
            f.write("keep")
        # Set old mtime to ensure it would be cleaned if matched
        old_mtime = time.time() - (CONFIG.max_cache_age_hours * 3600 + 3600)
        os.utime(other, (old_mtime, old_mtime))
        _cleanup_cache()
        assert os.path.exists(other)

    def test_handles_missing_directory(self, monkeypatch):
        monkeypatch.setattr("search.DOWNLOAD_DIR", "/nonexistent/path/xyz789")
        # Should not raise
        _cleanup_cache()
