import asyncio
import json
import shutil
import sys
from dataclasses import dataclass
from typing import List, Optional


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


async def extract_audio_url(video_url: str) -> tuple[Optional[str], Optional[str]]:
    """Extracts the direct audio-stream URL for a YouTube video.

    Returns ``(url, None)`` on success, ``(None, error_msg)`` on failure.
    """
    stdout, stderr = await _run_ytdlp(
        "--dump-json",
        "-f", "bestaudio",
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
        if url:
            return url, None
        return None, "No audio stream URL found"
    except json.JSONDecodeError:
        return None, "Failed to parse stream data"
