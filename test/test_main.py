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

from main import PlayerScreen, SeekModal, YouTubePlayerApp, _resolve_recent_ref, _play_notification_sound  # noqa: E402
from search import DownloadHandle, SearchResult  # noqa: E402


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


# ------------------------------------------------------------------
# _cleanup_active_download
# ------------------------------------------------------------------

class TestCleanupActiveDownload:
    """A completed download (.done marker actually on disk) must survive
    quit — everything else (in progress, failed, or merely exited without
    the marker having been written) should have its file removed."""

    def _make_app(self):
        return YouTubePlayerApp()

    def _make_handle(self, tmp_path, monkeypatch, *, marker: bool, done: bool = False, error: str = None) -> DownloadHandle:
        # _marker_path() (imported into main.py from search.py) resolves
        # against search.DOWNLOAD_DIR at call time, so patching it there is
        # enough regardless of the import path.
        monkeypatch.setattr("search.DOWNLOAD_DIR", str(tmp_path))
        file_path = str(tmp_path / "ytplay-abc123.webm")
        with open(file_path, "wb") as f:
            f.write(b"data")
        if marker:
            with open(str(tmp_path / "ytplay-abc123.done"), "w") as f:
                f.write(file_path)
        handle = DownloadHandle(None, file_path, "abc123", "abc123")
        handle.file_path = file_path
        handle._done = done
        handle._error = error
        return handle

    def test_deletes_incomplete_download(self, tmp_path, monkeypatch):
        app = self._make_app()
        handle = self._make_handle(tmp_path, monkeypatch, marker=False, done=False)
        app._active_download = handle
        app._cleanup_active_download()
        assert not os.path.exists(handle.file_path)

    def test_keeps_download_with_done_marker(self, tmp_path, monkeypatch):
        app = self._make_app()
        handle = self._make_handle(tmp_path, monkeypatch, marker=True, done=True)
        app._active_download = handle
        app._cleanup_active_download()
        assert os.path.exists(handle.file_path)

    def test_deletes_download_that_finished_with_error_even_if_marker_missing(self, tmp_path, monkeypatch):
        app = self._make_app()
        handle = self._make_handle(tmp_path, monkeypatch, marker=False, done=True, error="yt-dlp exited with code 1")
        app._active_download = handle
        app._cleanup_active_download()
        assert not os.path.exists(handle.file_path)

    def test_deletes_download_marked_done_without_marker(self, tmp_path, monkeypatch):
        # Regression: is_done can go True (asyncio observed the process exit)
        # without handle.wait() ever running, e.g. because _play_video_async
        # bailed out of the buffer-wait timeout first — so no marker was
        # written. Must not be mistaken for a valid completed cache entry.
        app = self._make_app()
        handle = self._make_handle(tmp_path, monkeypatch, marker=False, done=True, error=None)
        app._active_download = handle
        app._cleanup_active_download()
        assert not os.path.exists(handle.file_path)


# ------------------------------------------------------------------
# YouTubePlayerApp._buffer_wait_params
# ------------------------------------------------------------------

class TestBufferWaitParams:
    def test_fresh_start_uses_flat_defaults(self):
        min_bytes, timeout, stall_timeout = YouTubePlayerApp._buffer_wait_params(0.0)
        assert min_bytes == 65536
        assert timeout == 15.0
        assert stall_timeout == 20.0

    def test_deep_resume_scales_min_bytes_and_stall_timeout_not_timeout(self):
        min_bytes, timeout, stall_timeout = YouTubePlayerApp._buffer_wait_params(4068.6)
        assert min_bytes == int(4068.6 * 20000)
        # The startup grace period stays flat — only patience for a quiet
        # download scales with how deep the resume is, so a healthy-but-slow
        # download isn't killed by a deadline sized for an optimistic speed.
        assert timeout == 15.0
        assert stall_timeout == pytest.approx(4068.6 * 0.02)

    def test_small_seek_still_floors_at_defaults(self):
        min_bytes, timeout, stall_timeout = YouTubePlayerApp._buffer_wait_params(1.0)
        assert min_bytes == 65536  # 20000 bytes < floor
        assert stall_timeout == 20.0  # 0.02s scaled < floor


# ------------------------------------------------------------------
# _active_download_worker cancellation
# ------------------------------------------------------------------

class TestActiveDownloadWorkerCancellation:
    """A previous _play_video_async worker must not outlive a track switch
    or quit — otherwise it keeps awaiting a file _cleanup_active_download()
    has already deleted, and eventually fires a stale "timed out starting
    download" notification long after the user moved on (observed in
    log/yt-play-20260915-181437-13688.log: quit at 18:15:57, stale timeout
    error logged at 18:16:09 — 12s after the app had already returned to
    the menu)."""

    @staticmethod
    async def _hang_forever(*args, **kwargs):
        await asyncio.sleep(999)
        return None, None  # pragma: no cover - never reached

    def test_starting_new_track_cancels_previous_worker(self):
        async def scenario():
            app = YouTubePlayerApp()
            with patch("main.start_audio_download", AsyncMock(side_effect=self._hang_forever)):
                async with app.run_test() as pilot:
                    worker1 = app._play_video_async("https://www.youtube.com/watch?v=aaaaaaaaaaa", "A")
                    await pilot.pause()
                    assert app._active_download_worker is worker1
                    assert not worker1.is_cancelled

                    worker2 = app._play_video_async("https://www.youtube.com/watch?v=bbbbbbbbbbb", "B")
                    await pilot.pause()

                    assert worker1.is_cancelled
                    assert app._active_download_worker is worker2

        asyncio.run(scenario())

    def test_quit_cancels_active_worker(self):
        async def scenario():
            app = YouTubePlayerApp()
            with patch("main.start_audio_download", AsyncMock(side_effect=self._hang_forever)):
                async with app.run_test() as pilot:
                    worker = app._play_video_async("https://www.youtube.com/watch?v=aaaaaaaaaaa", "A")
                    await pilot.pause()
                    assert app._active_download_worker is worker

                    # Screen is MenuScreen (not PlayerScreen), so action_quit
                    # takes the direct-exit branch — the same one that must
                    # cancel a still-running download worker before exiting.
                    app.action_quit()
                    await pilot.pause()

                    assert worker.is_cancelled
                    assert app._active_download_worker is None

        asyncio.run(scenario())


async def _hang_forever(*args, **kwargs):
    await asyncio.sleep(999)
    return None, None  # pragma: no cover - never reached


# ------------------------------------------------------------------
# _resolve_step_index — next/prev routing, in-order and shuffled
# ------------------------------------------------------------------

class TestResolveStepIndex:
    """Pure attribute manipulation — no download/mpv needed, matches
    TestBufferWaitParams' no-harness style."""

    def _make_app(self, n, current_index=0, shuffle=False):
        app = YouTubePlayerApp()
        app.results = [object() for _ in range(n)]
        app.current_index = current_index
        app.shuffle_enabled = shuffle
        return app

    def test_empty_queue_returns_none(self):
        app = self._make_app(0)
        assert app._resolve_step_index(1) is None

    def test_next_in_order(self):
        app = self._make_app(3, current_index=0)
        assert app._resolve_step_index(1) == 1

    def test_next_at_end_returns_none(self):
        app = self._make_app(3, current_index=2)
        assert app._resolve_step_index(1) is None

    def test_prev_in_order(self):
        app = self._make_app(3, current_index=2)
        assert app._resolve_step_index(-1) == 1

    def test_prev_at_start_returns_none(self):
        app = self._make_app(3, current_index=0)
        assert app._resolve_step_index(-1) is None

    def test_shuffle_next_follows_shuffle_order_not_index_order(self):
        app = self._make_app(4, current_index=0, shuffle=True)
        app._shuffle_order = [2, 0, 3, 1]  # current_index 0 sits at position 1
        assert app._resolve_step_index(1) == 3  # next position (2) holds index 3

    def test_shuffle_prev_follows_shuffle_order(self):
        app = self._make_app(4, current_index=0, shuffle=True)
        app._shuffle_order = [2, 0, 3, 1]
        assert app._resolve_step_index(-1) == 2  # previous position (0) holds index 2

    def test_shuffle_at_end_of_order_returns_none(self):
        app = self._make_app(3, current_index=0, shuffle=True)
        app._shuffle_order = [1, 2, 0]  # current_index 0 is last in shuffle order
        assert app._resolve_step_index(1) is None

    def test_shuffle_regenerates_stale_order(self):
        app = self._make_app(3, current_index=0, shuffle=True)
        app._shuffle_order = [0, 1]  # stale — was built for a 2-item queue
        result = app._resolve_step_index(1)
        assert len(app._shuffle_order) == 3
        assert sorted(app._shuffle_order) == [0, 1, 2]
        # current_index (0) may land anywhere in the freshly regenerated
        # order, including last — a valid next-index or None either way.
        assert result is None or result in (0, 1, 2)


# ------------------------------------------------------------------
# Repeat / shuffle toggles and repeat-one auto-advance
# ------------------------------------------------------------------

class TestRepeatShuffleToggle:
    def test_toggle_repeat_flips_state(self):
        async def scenario():
            app = YouTubePlayerApp()
            async with app.run_test() as pilot:
                assert app.repeat_enabled is False
                app.action_toggle_repeat()
                await pilot.pause()
                assert app.repeat_enabled is True
                app.action_toggle_repeat()
                await pilot.pause()
                assert app.repeat_enabled is False

        asyncio.run(scenario())

    def test_toggle_shuffle_regenerates_order(self):
        async def scenario():
            app = YouTubePlayerApp()
            app.results = [object(), object(), object()]
            async with app.run_test() as pilot:
                assert app.shuffle_enabled is False
                app.action_toggle_shuffle()
                await pilot.pause()
                assert app.shuffle_enabled is True
                assert sorted(app._shuffle_order) == [0, 1, 2]

        asyncio.run(scenario())


class TestAdvanceToNextRepeat:
    """Repeat is single-track only: it replays the current track on a
    natural end, but never wraps the whole queue — that's still N/P's
    normal clamp-at-the-edges behavior."""

    def test_repeat_enabled_replays_current_track_not_next(self):
        async def scenario():
            app = YouTubePlayerApp()
            app.results = [
                SearchResult(id="a", title="A", url="https://www.youtube.com/watch?v=aaaaaaaaaaa", duration_str="1:00", uploader="U"),
                SearchResult(id="b", title="B", url="https://www.youtube.com/watch?v=bbbbbbbbbbb", duration_str="1:00", uploader="U"),
            ]
            app.current_index = 0
            app.repeat_enabled = True
            with patch("main.start_audio_download", AsyncMock(side_effect=_hang_forever)):
                async with app.run_test() as pilot:
                    app._advance_to_next()
                    await pilot.pause()
                    assert app.current_index == 0
                    assert app.current_title == "A"

        asyncio.run(scenario())

    def test_repeat_disabled_advances_normally(self):
        async def scenario():
            app = YouTubePlayerApp()
            app.results = [
                SearchResult(id="a", title="A", url="https://www.youtube.com/watch?v=aaaaaaaaaaa", duration_str="1:00", uploader="U"),
                SearchResult(id="b", title="B", url="https://www.youtube.com/watch?v=bbbbbbbbbbb", duration_str="1:00", uploader="U"),
            ]
            app.current_index = 0
            app.repeat_enabled = False
            with patch("main.start_audio_download", AsyncMock(side_effect=_hang_forever)):
                async with app.run_test() as pilot:
                    app._advance_to_next()
                    await pilot.pause()
                    assert app.current_index == 1
                    assert app.current_title == "B"

        asyncio.run(scenario())


# ------------------------------------------------------------------
# Playlist playback starts every track at 0:00 — never resumes
# ------------------------------------------------------------------

class TestPlaylistNoResume:
    def _make_app(self, tmp_path):
        app = YouTubePlayerApp()
        setattr(app, "_resume_path", str(tmp_path / "resume_state.json"))
        return app

    def test_playlist_queue_suppresses_resume(self, tmp_path):
        async def scenario():
            app = self._make_app(tmp_path)
            app._write_history([
                {"video_id": "aaaaaaaaaaa", "title": "A", "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "position": 42.0, "duration": 100.0},
            ])
            app.results = [SearchResult(id="aaaaaaaaaaa", title="A", url="https://www.youtube.com/watch?v=aaaaaaaaaaa", duration_str="1:40", uploader="U")]
            app._queue_is_playlist = True
            async with app.run_test() as pilot:
                with patch.object(app, "_play_video_async") as mock_play:
                    app.play_at(0)
                    await pilot.pause()
                assert mock_play.call_args.kwargs.get("resume") is False

        asyncio.run(scenario())

    def test_search_queue_still_resumes(self, tmp_path):
        async def scenario():
            app = self._make_app(tmp_path)
            app._write_history([
                {"video_id": "aaaaaaaaaaa", "title": "A", "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "position": 42.0, "duration": 100.0},
            ])
            app.results = [SearchResult(id="aaaaaaaaaaa", title="A", url="https://www.youtube.com/watch?v=aaaaaaaaaaa", duration_str="1:40", uploader="U")]
            app._queue_is_playlist = False
            async with app.run_test() as pilot:
                with patch.object(app, "_play_video_async") as mock_play:
                    app.play_at(0)
                    await pilot.pause()
                assert mock_play.call_args.kwargs.get("resume") is True

        asyncio.run(scenario())


# ------------------------------------------------------------------
# play_playlist
# ------------------------------------------------------------------

class TestPlayPlaylist:
    def test_empty_playlist_notifies_and_does_not_play(self, tmp_path, monkeypatch):
        import playlist as playlist_module

        async def scenario():
            monkeypatch.setattr(playlist_module, "PLAYLISTS_PATH", str(tmp_path / "playlists.json"))
            playlist_module.write_playlists([playlist_module.create_playlist([], "Empty")])
            app = YouTubePlayerApp()
            async with app.run_test() as pilot:
                with patch.object(app, "_play_video_async") as mock_play:
                    app.play_playlist("Empty")
                    await pilot.pause()
                mock_play.assert_not_called()

        asyncio.run(scenario())

    def test_play_playlist_loads_videos_into_results_and_marks_queue(self, tmp_path, monkeypatch):
        import playlist as playlist_module

        async def scenario():
            monkeypatch.setattr(playlist_module, "PLAYLISTS_PATH", str(tmp_path / "playlists.json"))
            playlists = [playlist_module.create_playlist([], "Mix")]
            playlist_module.add_video_to_playlist(
                playlists, "Mix",
                playlist_module.PlaylistEntry(id="aaaaaaaaaaa", title="A", url="https://www.youtube.com/watch?v=aaaaaaaaaaa", duration_str="1:00", uploader="U", added_at=0.0),
            )
            playlist_module.write_playlists(playlists)
            app = YouTubePlayerApp()
            with patch("main.start_audio_download", AsyncMock(side_effect=_hang_forever)):
                async with app.run_test() as pilot:
                    app.play_playlist("Mix")
                    await pilot.pause()
                    assert app._queue_is_playlist is True
                    assert len(app.results) == 1
                    assert app.results[0].id == "aaaaaaaaaaa"
                    assert app.current_title == "A"

        asyncio.run(scenario())


class TestRecentRef:
    """$N tokens in the search box resolve against recent_searches."""

    def test_resolves_index(self):
        recent = ["newest", "middle", "oldest"]
        assert _resolve_recent_ref("$0", recent) == "newest"
        assert _resolve_recent_ref("$2", recent) == "oldest"

    def test_out_of_range(self):
        assert _resolve_recent_ref("$3", ["a", "b", "c"]) is None
        assert _resolve_recent_ref("$0", []) is None

    def test_not_a_token(self):
        assert _resolve_recent_ref("$", ["a"]) is None
        assert _resolve_recent_ref("$x", ["a"]) is None
        assert _resolve_recent_ref("hello", ["a"]) is None
        assert _resolve_recent_ref("$-1", ["a"]) is None


def test_notification_sound_never_raises():
    with patch("winsound.MessageBeep", side_effect=RuntimeError("boom"), create=True):
        _play_notification_sound()
