"""Tests for main.py — PlayerScreen._fmt, smoke tests.""" 

import os
import sys

import pytest

# Importing player.py adds mpv-lib/ to PATH and then imports python-mpv.
# Skip the entire module if player.py (and thus mpv) isn't available.
mpv_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mpv-lib")
os.environ["PATH"] = mpv_dir + os.pathsep + os.environ.get("PATH", "")
try:
    import player  # noqa: F401 — sets PATH, imports mpv
except OSError:
    pytest.skip("mpv DLL not available — skipping main tests", allow_module_level=True)

from main import PlayerScreen  # noqa: E402


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
            (1.5, "00:01"),   # floats truncated to int
            (59.9, "00:59"),
            (119.7, "01:59"),
        ],
    )
    def test_format(self, seconds, expected):
        assert PlayerScreen._fmt(seconds) == expected


class TestPlaybackHotPaths:
    """Smoke checks for app actions — no mpv instance needed."""

    def test_fmt_round_trip(self):
        """Verify _fmt is invertible-ish: parseable as H:MM:SS or MM:SS."""
        result = PlayerScreen._fmt(3661)
        assert ":" in result
        parts = result.split(":")
        assert len(parts) in (2, 3)
        # All parts parse to int
        for p in parts:
            int(p)
