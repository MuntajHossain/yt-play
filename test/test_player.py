"""Tests for player.py — MpvPlayer volume persistence."""

import os

import pytest

# Importing player.py adds mpv-lib/ to PATH and then imports python-mpv.
# Skip the entire module if player.py (and thus mpv) isn't available.
mpv_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mpv-lib")
os.environ["PATH"] = mpv_dir + os.pathsep + os.environ.get("PATH", "")
try:
    from player import MpvPlayer  # noqa: E402 — sets PATH, imports mpv
except OSError:
    pytest.skip("mpv DLL not available — skipping player tests", allow_module_level=True)


class TestVolumePersistence:
    """Volume must survive across tracks — each play() spins up a new mpv
    instance, so the value has to live on MpvPlayer itself, not on the
    (throwaway) mpv.MPV instance."""

    def test_default_volume_is_80(self):
        assert MpvPlayer().volume == 80

    def test_set_volume_persists_without_active_player(self):
        p = MpvPlayer()
        p.set_volume(30)
        assert p.volume == 30

    def test_set_volume_clamps_to_0_100(self):
        p = MpvPlayer()
        p.set_volume(150)
        assert p.volume == 100
        p.set_volume(-10)
        assert p.volume == 0

    def test_volume_up_down_persist_and_clamp(self):
        p = MpvPlayer()
        p.set_volume(95)
        p.volume_up(10)
        assert p.volume == 100
        p.set_volume(3)
        p.volume_down(10)
        assert p.volume == 0
