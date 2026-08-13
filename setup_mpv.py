"""Fetch the vendored libmpv Windows build into mpv-lib/.

mpv-lib/ is gitignored (libmpv-2.dll is ~112MB, over GitHub's 100MB file
limit — see CLAUDE.md "Windows-specific notes"), so it isn't checked into
git and must be pulled separately after cloning. Run this directly:

    uv run setup_mpv.py

main.py also calls fetch_mpv_lib() automatically on startup if mpv-lib/
is missing, so a plain `uv run main.py` on a fresh clone pulls it too.

Source: the community Windows builds linked from mpv.io's own download
page (https://mpv.io/installation/), hosted on SourceForge.

Extraction uses Windows' built-in `tar.exe` (bsdtar/libarchive, shipped
since Windows 10 1803+) rather than a Python 7z library — the archives
use the BCJ2 filter, which py7zr doesn't support.
"""
import concurrent.futures
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
import xml.etree.ElementTree as ET

MPV_LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mpv-lib")
REQUIRED_FILE = os.path.join(MPV_LIB_DIR, "libmpv-2.dll")

RSS_URL = "https://sourceforge.net/projects/mpv-player-windows/rss?path=/libmpv"
# Plain x86_64 build, not the "-v3" variant (that one requires an AVX2 CPU).
BUILD_NAME_RE = re.compile(r"^/libmpv/mpv-dev-x86_64-\d{8}-git-[0-9a-f]+\.7z$")

# SourceForge round-robins each request to a mirror (different host per
# request) but serves identical bytes and honors Range requests on all of
# them — verified with a 6-way concurrent Range-request test against every
# chunk landing on a different mirror, sha256 matched a plain download.
DOWNLOAD_THREADS = 6
MIN_CHUNKED_SIZE = 2 * 1024 * 1024  # below this, threading overhead isn't worth it


def _find_latest_build_url() -> str:
    with urllib.request.urlopen(RSS_URL, timeout=30) as resp:
        data = resp.read()
    root = ET.fromstring(data)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if BUILD_NAME_RE.match(title):
            link = item.findtext("link")
            if link:
                return link.strip()
    raise RuntimeError(f"No matching libmpv build found in RSS feed at {RSS_URL}")


def _content_length(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return int(resp.headers.get("Content-Length", 0))


def _download_single(url: str, dest: str) -> None:
    with urllib.request.urlopen(url, timeout=180) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length", 0))
        read = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            read += len(chunk)
            if total:
                print(f"\r  {read / 1e6:.1f} / {total / 1e6:.1f} MB", end="", flush=True)
    print()


def _download_range(url: str, dest: str, start: int, end: int, progress: list, lock: threading.Lock) -> None:
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        if resp.status != 206:
            raise RuntimeError(f"Server did not honor Range request (status={resp.status})")
        with open(dest, "r+b") as f:
            f.seek(start)
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                with lock:
                    progress[0] += len(chunk)
                    read, total = progress
                    print(f"\r  {read / 1e6:.1f} / {total / 1e6:.1f} MB", end="", flush=True)


def _download_chunked(url: str, dest: str, total: int) -> None:
    with open(dest, "wb") as f:
        f.truncate(total)

    chunk_size = -(-total // DOWNLOAD_THREADS)  # ceil division
    ranges = [
        (start, min(start + chunk_size, total) - 1)
        for start in range(0, total, chunk_size)
    ]

    progress = [0, total]
    lock = threading.Lock()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ranges)) as pool:
        futures = [pool.submit(_download_range, url, dest, s, e, progress, lock) for s, e in ranges]
        for future in concurrent.futures.as_completed(futures):
            future.result()  # re-raise any chunk's exception here
    print()


def _download(url: str, dest: str) -> None:
    print(f"Downloading {url}")
    total = _content_length(url)
    if total < MIN_CHUNKED_SIZE:
        _download_single(url, dest)
        return
    try:
        _download_chunked(url, dest, total)
    except Exception as e:
        print(f"\nChunked download failed ({e}), retrying single-threaded...")
        _download_single(url, dest)


def _extract(archive_path: str, dest_dir: str) -> None:
    tar_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe")
    if not os.path.exists(tar_exe):
        raise RuntimeError(
            f"{tar_exe} not found (expected Windows' built-in bsdtar, present since "
            f"Windows 10 1803+). Extract {archive_path} into {dest_dir} manually instead."
        )
    print("Extracting...")
    subprocess.run([tar_exe, "-xf", archive_path, "-C", dest_dir], check=True)


def fetch_mpv_lib(force: bool = False) -> None:
    """Download and extract libmpv into mpv-lib/ if not already present."""
    if os.path.exists(REQUIRED_FILE) and not force:
        print(f"mpv-lib/ already present ({REQUIRED_FILE}) — skipping. Pass --force to re-fetch.")
        return

    os.makedirs(MPV_LIB_DIR, exist_ok=True)
    url = _find_latest_build_url()

    with tempfile.TemporaryDirectory() as tmp:
        archive_path = os.path.join(tmp, "libmpv.7z")
        _download(url, archive_path)
        _extract(archive_path, MPV_LIB_DIR)

    if not os.path.exists(REQUIRED_FILE):
        raise RuntimeError(
            f"Extraction finished but {REQUIRED_FILE} is still missing — "
            "the archive layout may have changed upstream."
        )
    print(f"mpv-lib/ ready ({REQUIRED_FILE}).")


if __name__ == "__main__":
    fetch_mpv_lib(force="--force" in sys.argv)
