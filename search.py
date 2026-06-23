import asyncio
import glob
import json
import logging
import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple

log = logging.getLogger("yt-play")

# Downloaded audio files are stored here, relative to wherever the app is
# run from (project root) - matches how main.py creates "log" relatively.
DOWNLOAD_DIR = "data"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


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

    def __init__(self, process: "asyncio.subprocess.Process", dest_template: str, file_id: str):
        self.process = process
        self.dest_template = dest_template
        self.file_id = file_id
        self.file_path: Optional[str] = None  # resolved once yt-dlp creates the real file
        self.started_at = time.monotonic()
        self._done = False
        self._error: Optional[str] = None

    @property
    def is_done(self) -> bool:
        if self._done:
            return True
        if self.process.returncode is not None:
            self._done = True
        return self._done

    @property
    def error(self) -> Optional[str]:
        return self._error

    async def wait(self) -> Optional[str]:
        """Wait for the download to finish. Returns an error string, or None on success."""
        _stdout, stderr = await self.process.communicate()
        self._done = True
        if self.process.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace")[-400:] if stderr else ""
            self._error = err_text.strip() or f"yt-dlp exited with code {self.process.returncode}"
            log.error("DOWNLOAD failed: %s", self._error)
        else:
            log.info("DOWNLOAD finished OK: %s", self.file_path)
        return self._error

    def kill(self):
        if self.process.returncode is None:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass


async def start_audio_download(video_url: str) -> Tuple[Optional[DownloadHandle], Optional[str]]:
    """Start downloading *video_url*'s audio to a local temp file.

    Returns ``(handle, None)`` on successful launch (download continues in the
    background; check handle.is_done / await handle.wait() for completion),
    or ``(None, error_msg)`` if the download could not even be started.

    Important: this intentionally does NOT use --extract-audio/--audio-format,
    because those trigger an ffmpeg post-process step that only writes the
    final output file after the *entire* source has downloaded - which would
    defeat the purpose of playing while downloading (the file would appear
    to do nothing until 100% complete, then suddenly exist). Instead we let
    yt-dlp write the natively-downloaded audio stream straight to disk in
    its original container, growing the file in real time as bytes arrive,
    so mpv can start reading from it almost immediately.
    """
    file_id = uuid.uuid4().hex
    # %(ext)s resolves to the actual container yt-dlp downloads (m4a/webm/etc).
    # We discover the real path afterwards by globbing, since we don't know
    # the extension until the format is chosen.
    dest_template = os.path.join(DOWNLOAD_DIR, f"ytplay-{file_id}.%(ext)s")

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

    handle = DownloadHandle(process, dest_template, file_id)
    # Discover the real on-disk path (with actual extension) as soon as it appears.
    found_path = await _wait_for_download_path(file_id, timeout=10.0)
    if found_path:
        handle.file_path = found_path
        log.info("DOWNLOAD file path resolved: %s", found_path)
    else:
        log.warning("DOWNLOAD file not yet visible on disk after launch (file_id=%s)", file_id)

    return handle, None


async def _wait_for_download_path(file_id: str, timeout: float = 10.0) -> Optional[str]:
    """Poll the download dir for the file yt-dlp actually created for *file_id*."""
    deadline = time.monotonic() + timeout
    pattern = os.path.join(DOWNLOAD_DIR, f"ytplay-{file_id}.*")
    while time.monotonic() < deadline:
        matches = [
            p for p in glob.glob(pattern)
            if not p.endswith(".part") and not p.endswith(".ytdl")
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