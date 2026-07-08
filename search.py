import asyncio
import glob
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple

from config import CONFIG

log = logging.getLogger("yt-play")

# Downloaded audio files are stored here, relative to wherever the app is
# run from (project root) - matches how main.py creates "log" relatively.
DOWNLOAD_DIR = "data"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def _extract_video_id(url: str) -> Optional[str]:
    """Extract YouTube video ID from common URL formats."""
    match = re.search(r"[?&]v=([a-zA-Z0-9_-]{11})", url)
    if match:
        return match.group(1)
    match = re.search(r"youtu\.be/([a-zA-Z0-9_-]{11})", url)
    if match:
        return match.group(1)
    return None


def _marker_path(video_id: str) -> str:
    return os.path.join(DOWNLOAD_DIR, f"ytplay-{video_id}.done")


def _check_cache(video_id: str) -> Optional[str]:
    """Return cached file path if valid (.done marker + audio file exist), else None."""
    marker = _marker_path(video_id)
    if not os.path.exists(marker):
        return None
    pattern = os.path.join(DOWNLOAD_DIR, f"ytplay-{video_id}.*")
    matches = [p for p in glob.glob(pattern) if not p.endswith(".done")]
    if matches:
        return matches[0]
    # Stale marker — file was manually deleted.
    os.remove(marker)
    return None


def _cleanup_cache() -> None:
    """Delete expired cached files and orphans not in resume history."""
    now = time.time()
    age_cutoff = now - CONFIG.max_cache_age_hours * 3600

    active_ids: set = set()
    resume_path = os.path.join(DOWNLOAD_DIR, "resume_state.json")
    try:
        if os.path.exists(resume_path):
            with open(resume_path) as f:
                history = json.load(f)
            if isinstance(history, list):
                for entry in history:
                    vid = entry.get("video_id")
                    if vid:
                        active_ids.add(vid)
    except Exception:
        log.exception("CACHE cleanup failed to read resume history")

    try:
        entries = os.listdir(DOWNLOAD_DIR)
        audio_ids: set = {
            m.group(1)
            for fn in entries
            if not fn.endswith(".done") and not fn.endswith(".part") and not fn.endswith(".ytdl")
            and (m := re.match(r"^ytplay-([a-zA-Z0-9_-]{11})\..+", fn))
        }

        for fname in entries:
            fpath = os.path.join(DOWNLOAD_DIR, fname)
            if not os.path.isfile(fpath) or not fname.startswith("ytplay-"):
                continue
            if fname.endswith(".part") or fname.endswith(".ytdl"):
                continue

            m = re.match(r"^ytplay-([a-zA-Z0-9_-]{11})\..+", fname)
            vid = m.group(1) if m else None
            mtime = os.path.getmtime(fpath)

            remove = False
            if fname.endswith(".done"):
                if vid not in audio_ids:
                    remove = True
                    reason = "orphan marker (no matching audio file)"
            elif vid and vid not in active_ids:
                remove = True
                reason = "orphan (not in history)"
            elif vid and vid in active_ids and mtime < age_cutoff:
                remove = True
                reason = f"expired (age={(now - mtime) / 3600:.1f}h)"
            elif not vid and mtime < age_cutoff:
                remove = True
                reason = f"unrecognized id, expired (age={(now - mtime) / 3600:.1f}h)"

            if remove:
                os.remove(fpath)
                log.info("CACHE cleanup removed %s: %s", reason, fname)
    except OSError:
        log.exception("CACHE cleanup error")


def _remove_partial(video_id: str) -> None:
    """Remove any partial download files for video_id (no .done marker)."""
    pattern = os.path.join(DOWNLOAD_DIR, f"ytplay-{video_id}.*")
    for fpath in glob.glob(pattern):
        if fpath.endswith(".done"):
            continue
        try:
            os.remove(fpath)
            log.info("CACHE removed partial download: %s", fpath)
        except OSError:
            log.exception("CACHE failed to remove partial: %s", fpath)


@dataclass
class SearchResult:
    id: str
    title: str
    url: str
    duration_str: str
    uploader: str

    @classmethod
    def from_dict(cls, data: dict) -> "SearchResult":
        duration = data.get("duration", 0)
        minutes, seconds = divmod(int(duration), 60)
        hours, minutes = divmod(minutes, 60)

        if hours > 0:
            duration_str = f"{hours}:{minutes:02d}:{seconds:02d}"
        else:
            duration_str = f"{minutes}:{seconds:02d}"

        return cls(
            id=data.get("id", ""),
            title=data.get("title", "Unknown Title"),
            url=data.get("webpage_url", ""),
            duration_str=duration_str,
            uploader=data.get("uploader", "Unknown Uploader"),
        )


def _ytdlp_cmd(*args: str) -> List[str]:
    """Return the command tuple to invoke yt-dlp.

    Prefers calling via ``python -m yt_dlp`` (faster, no shell spawn),
    and falls back to the ``yt-dlp`` binary found on PATH.
    """
    # If yt-dlp is importable, run it as a module – avoids a shell exec.
    try:
        import yt_dlp  # noqa: F401 – we only test importability
        return [sys.executable, "-m", "yt_dlp", *args]
    except ImportError:
        pass

    ytdlp_exe = shutil.which("yt-dlp")
    if ytdlp_exe:
        return [ytdlp_exe, *args]

    raise RuntimeError(
        "yt-dlp is not installed. Run: uv pip install yt-dlp"
    )


async def _run_ytdlp(*args: str) -> tuple[bytes, bytes]:
    """Run yt-dlp asynchronously and return (stdout, stderr)."""
    cmd = _ytdlp_cmd(*args)
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return await process.communicate()


async def search_youtube(query: str, max_results: int = 10) -> List[SearchResult]:
    """Searches YouTube via yt-dlp and returns structured results."""
    stdout, _stderr = await _run_ytdlp(
        "--dump-json",
        "--default-search",
        f"ytsearch{max_results}",
        "--no-playlist",
        "--ignore-errors",
        query,
    )

    results: List[SearchResult] = []
    if not stdout:
        return results

    for line in stdout.decode("utf-8").strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            results.append(SearchResult.from_dict(data))
        except json.JSONDecodeError:
            continue

    return results


# Format selection notes:
#   "bestaudio" lets yt-dlp pick whatever it considers best, which on YouTube
#   is very often a DASH-fragmented stream (webm/opus served via googlevideo's
#   DASH manifest). DASH segments respond to byte-range/seek requests less
#   predictably than a single progressive HTTP file, which is what caused
#   seeks past the buffered range to stall instead of erroring cleanly.
#
#   We prefer, in order:
#     1) a progressive (non-fragmented) HTTPS audio format - most reliable
#        for arbitrary seeking, since it's one continuous byte-range-seekable
#        file rather than a sequence of fetched segments.
#     2) any HTTPS bestaudio as a fallback if no progressive option exists.
#     3) plain bestaudio as a last resort (matches old behavior).
AUDIO_FORMAT_SELECTOR = (
    "bestaudio[protocol^=https][acodec!=none]/"
    "bestaudio[protocol^=http][acodec!=none]/"
    "bestaudio"
)


async def extract_audio_url(video_url: str) -> tuple[Optional[str], Optional[str]]:
    """Extracts the direct audio-stream URL for a YouTube video.

    Returns ``(url, None)`` on success, ``(None, error_msg)`` on failure.
    """
    stdout, stderr = await _run_ytdlp(
        "--dump-json",
        "-f", AUDIO_FORMAT_SELECTOR,
        video_url,
    )

    if not stdout:
        err = stderr.decode("utf-8", errors="replace")[:200] if stderr else ""
        if "private" in err.lower():
            return None, "Video is private"
        if "copyright" in err.lower() or "removed" in err.lower():
            return None, "Video unavailable (copyright / removed)"
        if "age" in err.lower() or "restrict" in err.lower():
            return None, "Age-restricted content"
        if err:
            return None, err.strip()
        return None, "No audio stream available"

    try:
        data = json.loads(stdout.decode("utf-8").strip())
        url = data.get("url")
        log.info(
            "FORMAT selected: format_id=%s ext=%s acodec=%s protocol=%s abr=%s",
            data.get("format_id"), data.get("ext"), data.get("acodec"),
            data.get("protocol"), data.get("abr"),
        )
        if url:
            return url, None
        return None, "No audio stream URL found"
    except json.JSONDecodeError:
        return None, "Failed to parse stream data"


# ---------------------------------------------------------------------------
# Download-to-disk-and-play
# ---------------------------------------------------------------------------
#
# Streaming directly from YouTube's CDN URL (extract_audio_url above) plays
# fine in order, but seeking ahead of what mpv has buffered/cached over the
# network can stall indefinitely (see player.py's stall watchdog). Local
# files don't have this problem: once bytes are on disk, mpv can seek to any
# point in them instantly, with zero network dependency for that seek.
#
# The approach here: launch yt-dlp as a background subprocess that downloads
# the audio to a local file, and return the destination path immediately
# (before the download finishes). mpv is able to play a file that is still
# growing on disk - it polls the file size and keeps reading as more data
# is appended, so playback can start almost immediately while the rest
# downloads in the background. Seeking within the already-downloaded portion
# is instant and 100% reliable; seeking ahead of what's downloaded so far
# will simply wait for the download to reach that point (which is much
# faster than re-opening a network stream, and recoverable by definition
# since the data is guaranteed to keep arriving).


class DownloadHandle:
    """Tracks a background yt-dlp download-to-file in progress."""

    def __init__(self, process: Optional["asyncio.subprocess.Process"], dest_template: str, file_id: str, video_id: str, is_cached: bool = False):
        self.process = process
        self.dest_template = dest_template
        self.file_id = file_id
        self.video_id = video_id
        self.file_path: Optional[str] = None  # resolved once yt-dlp creates the real file
        self.started_at = time.monotonic()
        self._done = is_cached
        self._error: Optional[str] = None
        self._is_cached = is_cached

    @property
    def is_cached(self) -> bool:
        return self._is_cached

    @property
    def is_done(self) -> bool:
        if self._done:
            return True
        if self.process is not None and self.process.returncode is not None:
            self._done = True
        return self._done

    @property
    def error(self) -> Optional[str]:
        return self._error

    async def wait(self) -> Optional[str]:
        """Wait for the download to finish. Returns an error string, or None on success."""
        if self._is_cached:
            return None
        _stdout, stderr = await self.process.communicate()
        self._done = True
        if self.process.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace")[-400:] if stderr else ""
            self._error = err_text.strip() or f"yt-dlp exited with code {self.process.returncode}"
            log.error("DOWNLOAD failed: %s", self._error)
        else:
            log.info("DOWNLOAD finished OK: %s", self.file_path)
            # Mark as cached for future plays.
            if self.video_id and self.file_path:
                marker = _marker_path(self.video_id)
                try:
                    with open(marker, "w") as f:
                        f.write(self.file_path)
                    log.info("CACHE marker created: %s", marker)
                except OSError:
                    log.exception("CACHE failed to write marker: %s", marker)
        return self._error

    def kill(self):
        p = self.process
        if p is not None and p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass
        # Drain pipes to prevent "unclosed transport" warnings on Windows (Python 3.14+).
        if p is not None:
            for stream in (p.stdout, p.stderr):
                if stream is None:
                    continue
                try:
                    stream.close()
                except Exception:
                    pass
            try:
                p.wait()
            except Exception:
                pass


async def start_audio_download(video_url: str) -> Tuple[Optional[DownloadHandle], Optional[str]]:
    """Start downloading *video_url*'s audio to a local temp file.

    Checks the local cache first — if a completed download for this video
    already exists, returns a cached handle immediately (no subprocess).

    Otherwise starts yt-dlp as a background subprocess using the video's
    YouTube ID for the filename, so subsequent plays can reuse the file.

    Returns ``(handle, None)`` on success, ``(None, error_msg)`` on failure.
    The returned handle may represent either a fresh download (check
    handle.is_cached) or a cache hit.

    Important: this intentionally does NOT use --extract-audio/--audio-format,
    because those trigger an ffmpeg post-process step that only writes the
    final output file after the *entire* source has downloaded - which would
    defeat the purpose of playing while downloading (the file would appear
    to do nothing until 100% complete, then suddenly exist). Instead we let
    yt-dlp write the natively-downloaded audio stream straight to disk in
    its original container, growing the file in real time as bytes arrive,
    so mpv can start reading from it almost immediately.
    """
    # Prune expired entries before we touch the cache dir.
    _cleanup_cache()

    video_id = _extract_video_id(video_url)

    # Check for a completed download.
    if video_id:
        cached = _check_cache(video_id)
        if cached:
            log.info("CACHE hit: video=%s file=%s", video_id, cached)
            handle = DownloadHandle(None, cached, video_id, video_id, is_cached=True)
            handle.file_path = cached
            return handle, None

        # Partial / abandoned download without .done marker — clean up.
        _remove_partial(video_id)

    # Fall back to UUID if we couldn't parse the video ID.
    if not video_id:
        video_id = uuid.uuid4().hex

    # %(ext)s resolves to the actual container yt-dlp downloads (m4a/webm/etc).
    # We discover the real path afterwards by globbing, since we don't know
    # the extension until the format is chosen.
    dest_template = os.path.join(DOWNLOAD_DIR, f"ytplay-{video_id}.%(ext)s")

    cmd = _ytdlp_cmd(
        "-f", AUDIO_FORMAT_SELECTOR,
        "--no-playlist",
        "--no-part",
        "-o", dest_template,
        video_url,
    )

    log.info("DOWNLOAD starting: url=%s dest_template=%s", video_url, dest_template)
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        log.exception("DOWNLOAD failed to launch yt-dlp")
        return None, f"Failed to start download: {e}"

    handle = DownloadHandle(process, dest_template, video_id, video_id)
    # Discover the real on-disk path (with actual extension) as soon as it appears.
    found_path = await _wait_for_download_path(video_id, timeout=10.0)
    if found_path:
        handle.file_path = found_path
        log.info("DOWNLOAD file path resolved: %s", found_path)
    else:
        log.warning("DOWNLOAD file not yet visible on disk after launch (video_id=%s)", video_id)

    return handle, None


async def _wait_for_download_path(video_id: str, timeout: float = 10.0) -> Optional[str]:
    """Poll the download dir for the file yt-dlp actually created for *video_id*."""
    deadline = time.monotonic() + timeout
    pattern = os.path.join(DOWNLOAD_DIR, f"ytplay-{video_id}.*")
    while time.monotonic() < deadline:
        matches = [
            p for p in glob.glob(pattern)
            if not p.endswith(".part") and not p.endswith(".ytdl") and not p.endswith(".done")
        ]
        if matches:
            return matches[0]
        await asyncio.sleep(0.1)
    return None


async def wait_for_file_growth(file_path: str, min_bytes: int = 65536, timeout: float = 15.0) -> bool:
    """Poll until *file_path* exists and has at least *min_bytes*, or timeout.

    Used to give the download a head start before handing the (still-growing)
    file to mpv, so playback doesn't immediately stall waiting on disk I/O
    that hasn't happened yet.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if os.path.exists(file_path) and os.path.getsize(file_path) >= min_bytes:
                return True
        except OSError:
            pass
        await asyncio.sleep(0.2)
    return False


async def fetch_video_title(url: str) -> str:
    """Fetch the video title from a YouTube URL via yt-dlp --dump-json.

    Returns the title string, or the URL itself if the request fails or times out.
    """
    cmd = _ytdlp_cmd("--dump-json", "--no-playlist", "--skip-download", url)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        info = json.loads(stdout.decode())
        return info.get("title") or url
    except Exception:
        log.exception("Failed to fetch title for %s", url)
        return url