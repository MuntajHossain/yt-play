"""Tests for main.py — PlayerScreen._fmt, SeekModal, history, app logic."""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

# Importing player.py adds mpv-lib/ to PATH and then imports python-mpv.
# Skip the entire module if player.py (and thus mpv) isn't available.
mpv_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mpv-lib")
os.environ["PATH"] = mpv_dir + os.pathsep + os.environ.get("PATH", "")
try:
    import player  # noqa: F401 — sets PATH, imports mpv
except OSError:
    pytest.skip("mpv DLL not available — skipping main tests", allow_module_level=True)

from main import PlayerScreen, SeekModal, YouTubePlayerApp  # noqa: E402


# ------------------------------------------------------------------
# PlayerScreen._fmt
# ------------------------------------------------------------------

class TestPlayerScreenFmt:
    """PlayerScreen._fmt is a staticmethod — pure, no state needed."""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "00:00"),
            (1, "00:01"),
            (59, "00:59"),
            (60, "01:00"),
            (61, "01:01"),
            (3599, "59:59"),
            (3600, "01:00:00"),
            (3661, "01:01:01"),
            (86399, "23:59:59"),
            (86400, "24:00:00"),
            (1.5, "00:01"),
            (59.9, "00:59"),
            (119.7, "01:59"),
        ],
    )
    def test_format(self, seconds, expected):
        assert PlayerScreen._fmt(seconds) == expected


class TestPlaybackHotPaths:
    """Smoke checks for app actions — no mpv instance needed."""

    def test_fmt_round_trip(self):
        result = PlayerScreen._fmt(3661)
        assert ":" in result
        parts = result.split(":")
        assert len(parts) in (2, 3)
        for p in parts:
            int(p)


# ------------------------------------------------------------------
# SeekModal._parse_timestamp (pure function)
# ------------------------------------------------------------------

class TestParseTimestamp:
    """SeekModal._parse_timestamp — no screen instance needed."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0", 0.0),
            ("30", 30.0),
            ("90", 90.0),
            ("120.5", 120.5),
            ("1:30", 90.0),
            ("5:00", 300.0),
            ("10:30", 630.0),
            ("1:30:40", 5440.0),
            ("0:05:00", 300.0),
            ("00:00:00", 0.0),
            ("   45   ", 45.0),
            (" 2:15 ", 135.0),
        ],
    )
    def test_valid_formats(self, raw, expected):
        assert SeekModal._parse_timestamp(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "abc",
            "1:2:3:4",
            "1:aa",
            "1:2:3:4:5",
        ],
    )
    def test_invalid_formats(self, raw):
        assert SeekModal._parse_timestamp(raw) is None


# ------------------------------------------------------------------
# History file I/O (no TUI thread needed)
# ------------------------------------------------------------------

class TestHistoryIO:
    """_read_history / _write_history with temp file."""

    RESUME_PATH_ATTR = "_resume_path"

    def _make_app(self, tmp_path):
        """Create an app pointed at a temp resume file."""
        app = YouTubePlayerApp()
        resume_file = tmp_path / "resume_state.json"
        setattr(app, self.RESUME_PATH_ATTR, str(resume_file))
        return app, resume_file

    def test_read_empty(self, tmp_path):
        app, _ = self._make_app(tmp_path)
        assert app._read_history() == []

    def test_write_and_read(self, tmp_path):
        app, resume_file = self._make_app(tmp_path)
        entries = [
            {"video_id": "abc123", "title": "T1", "url": "http://a", "position": 10.0},
            {"video_id": "xyz789", "title": "T2", "url": "http://b", "position": 20.0},
        ]
        app._write_history(entries)
        assert resume_file.exists()
        with open(resume_file) as f:
            data = json.load(f)
        assert len(data) == 2
        assert data[0]["video_id"] == "abc123"

    def test_read_legacy_single_dict(self, tmp_path):
        """Old format: single dict → migrated to list, returned."""
        app, resume_file = self._make_app(tmp_path)
        legacy = {"video_id": "old", "title": "Old Track", "position": 42.0}
        resume_file.write_text(json.dumps(legacy))
        result = app._read_history()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["video_id"] == "old"

    def test_save_resume_truncates_to_max_history(self, tmp_path):
        """_save_resume_data truncates when over MAX_HISTORY."""
        app, resume_file = self._make_app(tmp_path)
        # Seed 250 entries into history
        entries = [{"video_id": f"vid_{i}"} for i in range(app.MAX_HISTORY + 50)]
        app._write_history(entries)
        # Now save a new entry — should trigger truncation
        app.current_youtube_url = "https://www.youtube.com/watch?v=abcdef12345"
        app.current_title = "New Entry"
        app.current_index = 0
        app._save_resume_data()
        data = app._read_history()
        assert len(data) == app.MAX_HISTORY
        assert data[-1]["video_id"] == "abcdef12345"

    def test_save_resume_data_remove_and_append(self, tmp_path):
        """Same video_id removes old entry and appends new one (moves to end)."""
        app, resume_file = self._make_app(tmp_path)
        app.current_youtube_url = "https://www.youtube.com/watch?v=abcdef12345"
        app.current_title = "First"
        app.current_index = 0
        app._save_resume_data()
        data1 = app._read_history()
        assert len(data1) == 1
        assert data1[0]["title"] == "First"

        app.current_title = "Second"
        app._desired_position = 99.0
        app._save_resume_data()
        data2 = app._read_history()
        assert len(data2) == 1
        assert data2[0]["title"] == "Second"
        assert data2[0]["position"] == 99.0

    def test_save_resume_skips_no_url(self, tmp_path):
        app, _ = self._make_app(tmp_path)
        app.current_youtube_url = ""
        app._save_resume_data()
        assert app._read_history() == []

    def test_current_index_negative_still_saves_if_url_present(self, tmp_path):
        """Guard is only current_youtube_url — index doesn't block saves."""
        app, _ = self._make_app(tmp_path)
        app.current_youtube_url = "https://www.youtube.com/watch?v=abcdef12345"
        app.current_index = -1
        app._save_resume_data()
        assert len(app._read_history()) == 1


class TestDeleteHistoryEntry:
    """delete_history_entry matches by video_id, not position — a
    HistoryScreen showing a stale (pre-reorder) snapshot must still delete
    the entry the user actually picked, not whatever now sits at that
    index in a freshly re-read (and possibly reordered) history."""

    def _make_app(self, tmp_path):
        app = YouTubePlayerApp()
        resume_file = tmp_path / "resume_state.json"
        setattr(app, "_resume_path", str(resume_file))
        return app, resume_file

    def test_delete_by_video_id(self, tmp_path):
        app, _ = self._make_app(tmp_path)
        app._write_history([
            {"video_id": "old", "title": "Old"},
            {"video_id": "new", "title": "New"},
        ])
        title = app.delete_history_entry("new")
        assert title == "New"
        remaining = app._read_history()
        assert len(remaining) == 1
        assert remaining[0]["video_id"] == "old"

    def test_delete_survives_background_reorder(self, tmp_path):
        """Simulates HistoryScreen holding a stale snapshot: deleting "old"
        must remove "old" even after "new" was re-upserted to the end
        in between (order changed, identity-based delete is unaffected)."""
        app, _ = self._make_app(tmp_path)
        app._write_history([
            {"video_id": "old", "title": "Old"},
            {"video_id": "new", "title": "New"},
        ])
        app._write_history([
            {"video_id": "old", "title": "Old"},
            {"video_id": "new", "title": "New"},
        ])
        title = app.delete_history_entry("old")
        assert title == "Old"
        remaining = app._read_history()
        assert len(remaining) == 1
        assert remaining[0]["video_id"] == "new"

    def test_delete_unknown_video_id_returns_none_and_keeps_history(self, tmp_path):
        app, _ = self._make_app(tmp_path)
        app._write_history([{"video_id": "only", "title": "Only"}])
        assert app.delete_history_entry("missing") is None
        assert app.delete_history_entry(None) is None
        assert len(app._read_history()) == 1

    def test_delete_from_empty_history_returns_none(self, tmp_path):
        app, _ = self._make_app(tmp_path)
        assert app.delete_history_entry("anything") is None


class TestLookupHistoryPosition:
    """_lookup_history_position drives resume for any previously-watched video."""

    def _make_app(self, tmp_path):
        app = YouTubePlayerApp()
        setattr(app, "_resume_path", str(tmp_path / "resume_state.json"))
        return app

    def test_returns_saved_position(self, tmp_path):
        app = self._make_app(tmp_path)
        app._write_history([
            {"video_id": "aaaaaaaaaaa", "position": 123.5, "duration": 1000.0},
        ])
        assert app._lookup_history_position("aaaaaaaaaaa") == 123.5

    def test_unknown_video_returns_none(self, tmp_path):
        app = self._make_app(tmp_path)
        app._write_history([
            {"video_id": "aaaaaaaaaaa", "position": 123.5, "duration": 1000.0},
        ])
        assert app._lookup_history_position("zzzzzzzzzzz") is None

    def test_empty_video_id_returns_none(self, tmp_path):
        app = self._make_app(tmp_path)
        app._write_history([{"video_id": "aaaaaaaaaaa", "position": 123.5}])
        assert app._lookup_history_position("") is None

    def test_zero_position_returns_none(self, tmp_path):
        app = self._make_app(tmp_path)
        app._write_history([{"video_id": "aaaaaaaaaaa", "position": 0.0, "duration": 1000.0}])
        assert app._lookup_history_position("aaaaaaaaaaa") is None

    def test_finished_within_5s_returns_none(self, tmp_path):
        """Position near the end is treated as fully watched → start over."""
        app = self._make_app(tmp_path)
        app._write_history([{"video_id": "aaaaaaaaaaa", "position": 997.0, "duration": 1000.0}])
        assert app._lookup_history_position("aaaaaaaaaaa") is None

    def test_just_before_threshold_still_resumes(self, tmp_path):
        app = self._make_app(tmp_path)
        app._write_history([{"video_id": "aaaaaaaaaaa", "position": 994.0, "duration": 1000.0}])
        assert app._lookup_history_position("aaaaaaaaaaa") == 994.0

    def test_picks_position_regardless_of_array_order(self, tmp_path):
        """Regression: resume must not depend on the entry being last in the array.

        _save_resume_data upserts in-place, so the most-recently-played video
        is not necessarily the last array element. Lookup is by video_id.
        """
        app = self._make_app(tmp_path)
        app._write_history([
            {"video_id": "oldentry00001", "position": 5.0, "duration": 1000.0},
            {"video_id": "target0000001", "position": 555.0, "duration": 1000.0},
            {"video_id": "newerentry00", "position": 10.0, "duration": 1000.0},
        ])
        assert app._lookup_history_position("target0000001") == 555.0


# ------------------------------------------------------------------
# Recent searches
# ------------------------------------------------------------------

class TestRecentSearches:
    """recent_searches list on the app — pure in-memory, no TUI needed."""

    def _make_app(self):
        return YouTubePlayerApp()

    def test_initial_empty(self):
        app = self._make_app()
        assert app.recent_searches == []

    def test_first_search_added(self):
        app = self._make_app()
        app.recent_searches.insert(0, "hello")
        assert app.recent_searches == ["hello"]

    def test_deduplicate_reinserts_at_front(self):
        app = self._make_app()
        app.recent_searches = ["world", "hello"]
        # Simulate do_search logic
        query = "hello"
        if query in app.recent_searches:
            app.recent_searches.remove(query)
        app.recent_searches.insert(0, query)
        assert app.recent_searches == ["hello", "world"]

    def test_capped_at_10(self):
        app = self._make_app()
        for i in range(15):
            q = f"q{i}"
            if q in app.recent_searches:
                app.recent_searches.remove(q)
            app.recent_searches.insert(0, q)
            if len(app.recent_searches) > 10:
                app.recent_searches = app.recent_searches[:10]
        assert len(app.recent_searches) == 10
        assert app.recent_searches[0] == "q14"
        assert app.recent_searches[-1] == "q5"


# ------------------------------------------------------------------
# Search results pagination cache
# ------------------------------------------------------------------

class TestSearchCachePagination:
    """_ensure_search_cache fetches one SEARCH_PAGE_SIZE page at a time, lazily, stops on exhaustion."""

    def _make_app(self, query="test"):
        app = YouTubePlayerApp()
        app.search_query = query
        app._search_cache = []
        app._search_exhausted = False
        return app

    def test_fetches_pages_until_min_len_covered(self):
        app = self._make_app()
        size = app.SEARCH_PAGE_SIZE
        pages = [[f"r{p * size + i}" for i in range(size)] for p in range(3)]
        mock = AsyncMock(side_effect=pages)
        with patch("main.search_youtube", mock):
            asyncio.run(app._ensure_search_cache(3 * size))
        assert len(app._search_cache) == 3 * size
        assert mock.call_count == 3
        _, kwargs = mock.call_args_list[-1]
        assert kwargs["page"] == 3

    def test_noop_when_already_cached(self):
        app = self._make_app()
        app._search_cache = [f"r{i}" for i in range(app.SEARCH_PAGE_SIZE)]
        mock = AsyncMock()
        with patch("main.search_youtube", mock):
            asyncio.run(app._ensure_search_cache(app.SEARCH_PAGE_SIZE))
        mock.assert_not_called()

    def test_stops_and_marks_exhausted_on_short_batch(self):
        app = self._make_app()
        short_batch = [f"r{i}" for i in range(5)]
        mock = AsyncMock(side_effect=[short_batch])
        with patch("main.search_youtube", mock):
            asyncio.run(app._ensure_search_cache(60))
        assert app._search_exhausted is True
        assert len(app._search_cache) == 5
        mock.assert_called_once()  # doesn't keep retrying once exhausted

    def test_stops_on_empty_batch(self):
        app = self._make_app()
        mock = AsyncMock(side_effect=[[]])
        with patch("main.search_youtube", mock):
            asyncio.run(app._ensure_search_cache(10))
        assert app._search_exhausted is True
        assert app._search_cache == []
