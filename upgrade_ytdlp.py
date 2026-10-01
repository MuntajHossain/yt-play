"""Upgrade yt-dlp to the latest release in this project's uv-managed venv.

YouTube changes its site often enough that yt-dlp needs regular updates to
keep extracting playable audio streams — a stale yt-dlp is a common cause of
a download that starts but never produces data (see CLAUDE.md's "Known
issue" notes and the buffer-wait logic in main.py). Run this directly:

    uv run upgrade_ytdlp.py
"""
import subprocess
import sys


def _ytdlp_version() -> str:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except Exception as e:
        return f"unknown ({e})"


def main() -> None:
    before = _ytdlp_version()
    print(f"Current yt-dlp: {before}")
    print("Upgrading yt-dlp...")
    subprocess.run(["uv", "lock", "--upgrade-package", "yt-dlp"], check=True)
    subprocess.run(["uv", "sync"], check=True)
    after = _ytdlp_version()
    if after == before:
        print(f"yt-dlp already up to date: {after}")
    else:
        print(f"yt-dlp upgraded: {before} -> {after}")


if __name__ == "__main__":
    main()
