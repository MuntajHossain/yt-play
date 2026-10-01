"""Named playlists of videos, persisted to data/playlists.json.

Stateless on disk, same pattern as main.py's play-history I/O: every call
reads the current file fresh and callers write back after mutating — no
in-memory cache to go stale.
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional

log = logging.getLogger("yt-play")

PLAYLISTS_PATH = os.path.join("data", "playlists.json")


@dataclass
class PlaylistEntry:
    id: str
    title: str
    url: str
    duration_str: str
    uploader: str
    added_at: float


@dataclass
class Playlist:
    name: str
    created_at: float
    videos: List[PlaylistEntry] = field(default_factory=list)


def read_playlists() -> List[Playlist]:
    if not os.path.exists(PLAYLISTS_PATH):
        return []
    try:
        with open(PLAYLISTS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return [
            Playlist(
                name=p["name"],
                created_at=p.get("created_at", 0.0),
                videos=[PlaylistEntry(**v) for v in p.get("videos", [])],
            )
            for p in raw
        ]
    except Exception:
        log.exception("PLAYLIST failed to read %s", PLAYLISTS_PATH)
        return []


def write_playlists(playlists: List[Playlist]) -> None:
    try:
        parent = os.path.dirname(PLAYLISTS_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(PLAYLISTS_PATH, "w", encoding="utf-8") as f:
            json.dump([asdict(p) for p in playlists], f, indent=2)
    except Exception:
        log.exception("PLAYLIST failed to write %s", PLAYLISTS_PATH)


def get_playlist(playlists: List[Playlist], name: str) -> Optional[Playlist]:
    for p in playlists:
        if p.name == name:
            return p
    return None


def create_playlist(playlists: List[Playlist], name: str) -> Playlist:
    """Idempotent: returns the existing playlist if name is already taken."""
    existing = get_playlist(playlists, name)
    if existing is not None:
        return existing
    pl = Playlist(name=name, created_at=time.time(), videos=[])
    playlists.append(pl)
    return pl


def add_video_to_playlist(playlists: List[Playlist], name: str, entry: PlaylistEntry) -> bool:
    """Adds entry to the named playlist. Returns False if the playlist
    doesn't exist or the video is already in it (dedupe is per-playlist —
    the same video may still belong to other playlists)."""
    pl = get_playlist(playlists, name)
    if pl is None:
        return False
    if any(v.id == entry.id for v in pl.videos):
        return False
    pl.videos.append(entry)
    return True


def remove_video_from_playlist(playlists: List[Playlist], name: str, video_id: str) -> bool:
    pl = get_playlist(playlists, name)
    if pl is None:
        return False
    before = len(pl.videos)
    pl.videos = [v for v in pl.videos if v.id != video_id]
    return len(pl.videos) < before


def delete_playlist(playlists: List[Playlist], name: str) -> bool:
    before = len(playlists)
    playlists[:] = [p for p in playlists if p.name != name]
    return len(playlists) < before


def list_playlist_names(playlists: List[Playlist]) -> List[str]:
    return [p.name for p in playlists]
