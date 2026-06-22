import asyncio
import json
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
            uploader=data.get("uploader", "Unknown Uploader")
        )

async def search_youtube(query: str, max_results: int = 10) -> List[SearchResult]:
    """
    Searches YouTube using yt-dlp and returns a list of results.
    Runs asynchronously to avoid blocking the main thread.
    """
    cmd = [
        "uv", "run", "yt-dlp",
        "--dump-json",
        "--default-search", f"ytsearch{max_results}",
        "--no-playlist",
        "--ignore-errors",
        query
    ]
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    stdout, stderr = await process.communicate()
    
    results = []
    if stdout:
        for line in stdout.decode('utf-8').strip().split('\n'):
            if line:
                try:
                    data = json.loads(line)
                    results.append(SearchResult.from_dict(data))
                except json.JSONDecodeError:
                    continue
                    
    return results

async def extract_audio_url(video_url: str) -> Optional[str]:
    """
    Extracts the best audio URL from a video URL using yt-dlp.
    """
    cmd = [
        "uv", "run", "yt-dlp",
        "--dump-json",
        "-f", "bestaudio",
        video_url
    ]
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    stdout, stderr = await process.communicate()
    
    if stdout:
        try:
            data = json.loads(stdout.decode('utf-8').strip())
            return data.get("url")
        except json.JSONDecodeError:
            pass
            
    return None
