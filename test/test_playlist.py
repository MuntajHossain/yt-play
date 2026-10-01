"""Tests for playlist.py — playlist CRUD and per-playlist video dedupe."""

import os

import playlist as playlist_module
from playlist import (
    Playlist,
    PlaylistEntry,
    read_playlists,
    write_playlists,
    get_playlist,
    create_playlist,
    add_video_to_playlist,
    remove_video_from_playlist,
    delete_playlist,
    list_playlist_names,
)


def _entry(vid="v1", title="Video 1"):
    return PlaylistEntry(id=vid, title=title, url=f"https://youtu.be/{vid}", duration_str="3:00", uploader="Uploader", added_at=0.0)


# ---------------------------------------------------------------------------
# read/write
# ---------------------------------------------------------------------------

class TestReadWritePlaylists:
    def test_read_missing_file_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(playlist_module, "PLAYLISTS_PATH", str(tmp_path / "playlists.json"))
        assert read_playlists() == []

    def test_write_then_read_round_trips(self, monkeypatch, tmp_path):
        monkeypatch.setattr(playlist_module, "PLAYLISTS_PATH", str(tmp_path / "playlists.json"))
        pl = create_playlist([], "Mix")
        add_video_to_playlist([pl], "Mix", _entry())
        write_playlists([pl])

        loaded = read_playlists()
        assert len(loaded) == 1
        assert loaded[0].name == "Mix"
        assert len(loaded[0].videos) == 1
        assert loaded[0].videos[0].id == "v1"

    def test_read_tolerates_corrupt_file(self, monkeypatch, tmp_path):
        path = tmp_path / "playlists.json"
        path.write_text("not json")
        monkeypatch.setattr(playlist_module, "PLAYLISTS_PATH", str(path))
        assert read_playlists() == []


# ---------------------------------------------------------------------------
# create/get/delete
# ---------------------------------------------------------------------------

class TestCreateGetDeletePlaylist:
    def test_create_new_playlist(self):
        playlists = []
        pl = create_playlist(playlists, "Mix")
        assert pl.name == "Mix"
        assert pl.videos == []
        assert playlists == [pl]

    def test_create_existing_playlist_is_idempotent(self):
        playlists = []
        first = create_playlist(playlists, "Mix")
        add_video_to_playlist(playlists, "Mix", _entry())
        second = create_playlist(playlists, "Mix")
        assert second is first
        assert len(playlists) == 1
        assert len(second.videos) == 1

    def test_get_playlist_missing_returns_none(self):
        assert get_playlist([], "Nope") is None

    def test_delete_playlist(self):
        playlists = []
        create_playlist(playlists, "Mix")
        assert delete_playlist(playlists, "Mix") is True
        assert playlists == []

    def test_delete_missing_playlist_returns_false(self):
        assert delete_playlist([], "Nope") is False

    def test_list_playlist_names(self):
        playlists = []
        create_playlist(playlists, "A")
        create_playlist(playlists, "B")
        assert list_playlist_names(playlists) == ["A", "B"]


# ---------------------------------------------------------------------------
# add/remove video
# ---------------------------------------------------------------------------

class TestAddRemoveVideo:
    def test_add_video_to_playlist(self):
        playlists = []
        create_playlist(playlists, "Mix")
        assert add_video_to_playlist(playlists, "Mix", _entry()) is True
        assert len(get_playlist(playlists, "Mix").videos) == 1

    def test_add_video_dedupes_within_same_playlist(self):
        playlists = []
        create_playlist(playlists, "Mix")
        add_video_to_playlist(playlists, "Mix", _entry())
        assert add_video_to_playlist(playlists, "Mix", _entry()) is False
        assert len(get_playlist(playlists, "Mix").videos) == 1

    def test_add_video_to_missing_playlist_returns_false(self):
        assert add_video_to_playlist([], "Nope", _entry()) is False

    def test_same_video_can_be_in_multiple_playlists(self):
        playlists = []
        create_playlist(playlists, "A")
        create_playlist(playlists, "B")
        add_video_to_playlist(playlists, "A", _entry())
        add_video_to_playlist(playlists, "B", _entry())
        assert len(get_playlist(playlists, "A").videos) == 1
        assert len(get_playlist(playlists, "B").videos) == 1

    def test_remove_video_from_playlist(self):
        playlists = []
        create_playlist(playlists, "Mix")
        add_video_to_playlist(playlists, "Mix", _entry())
        assert remove_video_from_playlist(playlists, "Mix", "v1") is True
        assert get_playlist(playlists, "Mix").videos == []

    def test_remove_missing_video_returns_false(self):
        playlists = []
        create_playlist(playlists, "Mix")
        assert remove_video_from_playlist(playlists, "Mix", "nope") is False

    def test_remove_video_from_missing_playlist_returns_false(self):
        assert remove_video_from_playlist([], "Nope", "v1") is False
