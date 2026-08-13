# yt-play

Terminal-based YouTube audio player. Search YouTube, build a queue, and play audio through mpv — all from the terminal.

## Requirements

- Windows
- Python ≥ 3.10
- `uv` (recommended) or pip

No separate mpv install needed — playback uses [libmpv](https://mpv.io/) directly via `python-mpv`, and the Windows build is fetched automatically into `mpv-lib/` on first run (~112MB, one-time; extraction is pure Python, no external tools).

## Install

```bash
uv sync
```

## Run

```bash
uv run main.py
```

First run downloads `mpv-lib/` automatically. To fetch it separately (or re-fetch), run `uv run setup_mpv.py`.

## Key Bindings

| Key | Action |
|---|---|
| `Ctrl+D` | Quit (with confirmation) |
| `Space` | Play / Pause |
| `Left / Right` | Seek ±5s |
| `Up / Down` | Volume ±5 |
| `N / P` | Next / Previous track |
| `[ / ]` | Speed ∓0.25x |
| `Esc` | Back to previous screen |
