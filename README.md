# yt-play

Terminal-based YouTube audio player. Search YouTube, build a queue, and play audio through mpv — all from the terminal.

## Requirements

- Python ≥ 3.10
- [mpv](https://mpv.io/) installed and on PATH
- `uv` (recommended) or pip

## Install

```bash
uv sync
```

## Run

```bash
uv run main.py
```

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
