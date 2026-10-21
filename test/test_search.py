"""Tests for search.py — video ID extraction, SearchResult, cache functions."""

import asyncio
import os
import time
from unittest.mock import patch

import pytest

import search as search_module
from search import (
    _extract_video_id,
    _marker_path,
    _check_cache,
    _cleanup_cache,
    _remove_partial,
    search_youtube,
    SearchResult,
    DownloadHandle,
    wait_for_file_growth,
)
from config import CONFIG


# ---------------------------------------------------------------------------
# DownloadHandle progress
# ---------------------------------------------------------------------------

class TestDownloadProgress:
    def test_parse_total_bytes(self):
        assert DownloadHandle.parse_progress("ytprog 500 1000 NA") == 50.0

    def test_parse_falls_back_to_estimate(self):
        assert DownloadHandle.parse_progress("ytprog 250 NA 1000") == 25.0

    def test_parse_unknown_total(self):
        assert DownloadHandle.parse_progress("ytprog 250 NA NA") is None

    def test_parse_clamps_over_100(self):
        assert DownloadHandle.parse_progress("ytprog 2000 1000 NA") == 100.0

    def test_parse_ignores_other_lines(self):
        assert DownloadHandle.parse_progress("[download] Destination: x.m4a") is None
        assert DownloadHandle.parse_progress("ytprog abc 1000 NA") is None

    def test_cached_handle_is_100(self):
        async def run():
            return DownloadHandle(None, "p", "vid", "vid", is_cached=True).percent
        assert asyncio.run(run()) == 100.0

    def test_stdout_reader_updates_percent_and_path(self):
        async def run():
            h = DownloadHandle(None, "p", "vid", "vid")
            reader = asyncio.StreamReader()
            reader.feed_data(b"[download] Destination: a.m4a\nytprog 100 400 NA\nytprog 300 400 NA\n")
            reader.feed_eof()

            class P:
                stdout = reader
                returncode = None
            h.process = P()
            await h._read_stdout()
            return h.file_path, h.percent
        path, pct = asyncio.run(run())
        assert path == "a.m4a"
        assert pct == 75.0


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
# search_youtube pagination
# ---------------------------------------------------------------------------

class TestSearchYoutubePagination:
    """search_youtube slices results via --playlist-start/--playlist-end."""

    def _capture_args(self):
        captured = {}

        async def fake_run_ytdlp(*args):
            captured["args"] = args
            return b"", b""

        return captured, fake_run_ytdlp

    def test_page_one_range(self):
        captured, fake = self._capture_args()
        with patch.object(search_module, "_run_ytdlp", fake):
            asyncio.run(search_youtube("test query", page=1, page_size=10))
        args = captured["args"]
        assert "ytsearch10" in args
        assert args[args.index("--playlist-start") + 1] == "1"
        assert args[args.index("--playlist-end") + 1] == "10"

    def test_page_two_range(self):
        captured, fake = self._capture_args()
        with patch.object(search_module, "_run_ytdlp", fake):
            asyncio.run(search_youtube("test query", page=2, page_size=10))
        args = captured["args"]
        assert "ytsearch20" in args
        assert args[args.index("--playlist-start") + 1] == "11"
        assert args[args.index("--playlist-end") + 1] == "20"

    def test_empty_stdout_returns_empty_list(self):
        captured, fake = self._capture_args()
        with patch.object(search_module, "_run_ytdlp", fake):
            results = asyncio.run(search_youtube("test query", page=3, page_size=5))
        assert results == []


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


# ---------------------------------------------------------------------------
# wait_for_file_growth
# ---------------------------------------------------------------------------

class TestWaitForFileGrowth:
    def test_returns_true_once_min_bytes_reached(self, tmp_path):
        path = str(tmp_path / "f.bin")
        with open(path, "wb") as f:
            f.write(b"x" * 100)
        result = asyncio.run(wait_for_file_growth(path, min_bytes=50, timeout=1.0))
        assert result is True

    def test_returns_false_when_file_never_appears(self, tmp_path):
        path = str(tmp_path / "missing.bin")
        result = asyncio.run(
            wait_for_file_growth(path, min_bytes=50, timeout=0.2, stall_timeout=0.2)
        )
        assert result is False

    def test_survives_slow_but_steady_growth_past_soft_timeout(self, tmp_path):
        # Regression: a download that hasn't reached min_bytes by the soft
        # `timeout` but is still actively growing must not be killed — only a
        # genuine stall (no growth for stall_timeout) should fail it.
        path = str(tmp_path / "growing.bin")

        async def trickle():
            for _ in range(6):
                await asyncio.sleep(0.1)
                with open(path, "ab") as f:
                    f.write(b"x" * 20)

        async def run():
            writer = asyncio.ensure_future(trickle())
            try:
                return await wait_for_file_growth(
                    path, min_bytes=100, timeout=0.15, stall_timeout=0.5
                )
            finally:
                await writer

        assert asyncio.run(run()) is True

    def test_fails_after_stall_timeout_with_no_growth(self, tmp_path):
        path = str(tmp_path / "stalled.bin")
        with open(path, "wb") as f:
            f.write(b"x" * 10)  # below min_bytes, and never grows further
        result = asyncio.run(
            wait_for_file_growth(path, min_bytes=1000, timeout=0.1, stall_timeout=0.2)
        )
        assert result is False


class TestFetchChannelVideos:
    def _fake(self, responses):
        calls = []

        async def fake_run_ytdlp(*args):
            calls.append(args)
            return responses.pop(0)

        return calls, fake_run_ytdlp

    def test_display_name_with_spaces_resolves_via_channel_search(self):
        search_hit = b'{"channel_url": "https://www.youtube.com/channel/UC123", "channel": "Mir Afsar Ali"}\n'
        videos = b'{"id": "abcdefghijk", "title": "T", "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk", "duration": 61}\n'
        calls, fake = self._fake([(search_hit, b""), (videos, b"")])
        with patch.object(search_module, "_run_ytdlp", fake):
            results = asyncio.run(search_module.fetch_channel_videos("Mir Afsar Ali"))
        assert "results?search_query=Mir+Afsar+Ali" in calls[0][-1]
        assert calls[1][-1] == "https://www.youtube.com/channel/UC123/videos"
        assert len(results) == 1
        assert results[0].uploader == "Mir Afsar Ali"
        assert results[0].duration_str == "1:01"

    def test_handle_without_spaces_tried_directly(self):
        direct = b'{"channel_url": "https://www.youtube.com/channel/UC9", "channel": "Real"}'
        videos = b'{"id": "abcdefghijk", "title": "T", "webpage_url": "u", "duration": 5}\n'
        calls, fake = self._fake([(direct, b""), (videos, b"")])
        with patch.object(search_module, "_run_ytdlp", fake):
            results = asyncio.run(search_module.fetch_channel_videos("realmir"))
        assert calls[0][-1] == "https://www.youtube.com/@realmir/videos"
        assert len(results) == 1

    def test_unresolvable_channel_returns_empty(self):
        calls, fake = self._fake([(b"", b"boom")])
        with patch.object(search_module, "_run_ytdlp", fake):
            results = asyncio.run(search_module.fetch_channel_videos("Mir Afsar Ali"))
        assert results == []


# ---------------------------------------------------------------------------
# fetch_video_title
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, out=b"", hang=False):
        self._out, self._hang = out, hang
        self.returncode = None
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        self.returncode = 0
        return self._out, b""

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class TestFetchVideoTitle:
    def _run(self, procs, timeout_patch=None):
        it = iter(procs)

        async def fake_exec(*a, **k):
            return next(it)

        async def go():
            with patch("asyncio.create_subprocess_exec", fake_exec):
                return await search_module.fetch_video_title("https://youtu.be/x")
        return asyncio.run(go())

    def test_returns_title(self):
        assert self._run([_FakeProc(b"My Song\n")]) == "My Song"

    def test_retries_after_empty_then_succeeds(self):
        assert self._run([_FakeProc(b"NA\n"), _FakeProc(b"Real\n")]) == "Real"

    def test_falls_back_to_url_when_empty_twice(self):
        assert self._run([_FakeProc(b""), _FakeProc(b"")]) == "https://youtu.be/x"

    def test_timeout_kills_process_and_falls_back(self):
        p = _FakeProc(hang=True)

        async def fake_exec(*a, **k):
            return p

        async def fake_wait_for(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError

        async def go():
            with patch("asyncio.create_subprocess_exec", fake_exec), \
                 patch("asyncio.wait_for", fake_wait_for):
                return await search_module.fetch_video_title("https://youtu.be/x")
        assert asyncio.run(go()) == "https://youtu.be/x"
        assert p.killed
