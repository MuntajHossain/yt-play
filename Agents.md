# Agents Configuration

This file outlines the configuration and details of the YouTube Player project for future AI development agents.

## Project Overview
A terminal-based YouTube audio player using `yt-dlp` to search and fetch audio streams, and `libmpv` (via `python-mpv`) for playback. The interface is built with the Textual TUI framework, using a **3-screen architecture** instead of a single cluttered view.

## Tech Stack
* **Language:** Python (3.10+)
* **Package Manager:** uv
* **Key Dependencies:**
  * `yt-dlp` - For searching YouTube and retrieving stream URLs.
  * `textual` - For the terminal user interface (screens, widgets, bindings).
  * `python-mpv` - libmpv bindings for audio playback.
  * `mpv` - External binary; must be on PATH.

## Project Structure
* [main.py](file:///c:/Users/hossa/Personal/Python/yt-play/main.py) - App orchestrator + 4 screen/modal classes (Search, Results, Player, SeekModal).
* [search.py](file:///c:/Users/hossa/Personal\Python\yt-play\search.py) - Async wrappers for running `yt-dlp` queries via `asyncio.create_subprocess_exec`.
* [player.py](file:///c:/Users/hossa\Personal\Python\yt-play\player.py) - MpvPlayer class wrapping `python-mpv` (libmpv) for audio playback.
* [config.py](file:///c:/Users/hossa/Personal\Python\yt-play\config.py) — Cache configuration (max age, dir).
* [.vscode/settings.json](file:///c:/Users/hossa/Personal/Python/yt-play/.vscode/settings.json) — Workspace settings (disables auto venv activation).
* [mpv-lib/](file:///c:/Users/hossa/Personal\Python\yt-play\mpv-lib/) — Contains `mpv-2.dll` (libmpv for python-mpv) and MinGW import libs.
* ~~[ui_components.py](file:///c:/Users/hossa/Personal\Python\yt-play\ui_components.py)~~ — **Deleted.** Widget logic moved into screen classes in `main.py`.

## Architecture: 3-Screen Navigation

The app uses Textual's `push_screen`/`pop_screen` stack:

```
[SearchScreen] → search → [ResultsScreen] → select → [PlayerScreen]
       ↑                      ↑  [Esc]                     [Esc]  │
       └──────────────────────┘                                track ends
                                                                or user pops
```

- **SearchScreen** — Centered input, no clutter. `Input.Submitted` triggers `app.do_search()`.
- **ResultsScreen** — Displays `OptionList` of search results. `Esc` pops back to search. Selection calls `app.play_at()`.
- **PlayerScreen** — Shows now-playing title, progress bar with time labels, controls help. Live updates from player callbacks.
- **QuitScreen** — Modal confirmation (`Ctrl+D`). Extends `ModalScreen[bool]`.
- **SeekModal** — Input modal to seek to exact timestamp (`g` key). Accepts H:MM:SS, M:SS, or seconds. Validates against track duration.
- **`@work` decorators** on async methods that dispatch background work (`do_search`, `_play_video_async`). These return `Worker` objects — do **not** `await` them. `do_search` uses `exclusive=True` (concurrent searches wasteful). `_play_video_async` intentionally avoids `exclusive=True` so N/P next/prev track works — old download cleaned up via `_cleanup_active_download()` inside the method.

## Setup & Installation
```bash
uv sync
```

## Running the Application
```bash
uv run main.py
```

## Key Bindings

| Key | Action |
|---|---|
| `Ctrl+D` | Quit (with confirmation) — avoids VS Code `Ctrl+Q` conflict |
| `Space` | Play / Pause |
| `Left / Right` | Seek ±5s |
| `G` | Go to position (H:MM:SS, M:SS, or seconds) |
| `Up / Down` | Volume ±5 |
| `N / P` | Next / Previous track |
| `[ / ]` | Speed ∓0.25x (range 0.25–3.0) |
| `Esc` | Back to previous screen |

Bindings are app-level, so playback controls work from any screen.

## Audio Download Cache

Audio files are cached locally using the YouTube video ID as the key. Files named `ytplay-{video_id}.{ext}` in `data/`. A `.done` marker file tracks completion.

**Cache flow:**
1. Before each download, prune files >7h old (`config.py` — `CacheConfig.max_cache_age_hours`)
2. Extract `video_id` from URL, check if `ytplay-{video_id}.done` + audio file exist
3. Cache hit → return handle with `is_cached=True`, no yt-dlp subprocess
4. Partial file (no `.done` marker) → delete and re-download 
5. After download completes → write `.done` marker via `DownloadHandle.wait()`
6. `main.py`'s `_cleanup_active_download()` skips cached files (doesn't delete them)

**Config:** `config.py` — `CacheConfig` dataclass with `cache_dir` and `max_cache_age_hours`.

## Resume Session (implemented)

Playback position saved to `data/resume_state.json`:
- On quit from PlayerScreen (via `action_quit()` callback)
- Every 5s during playback (`_update_player_progress` timer)

On next launch, if same video is selected from search results, position auto-restored. `_play_video_async()` checks `_resume_data` for matching `video_id`, overrides `seek_to`, and calls `self.player.play(path, seek_to=position)`.

**Seek deferred to playback-restart:** Initial seek is deferred via `_pending_seek` in `MpvPlayer`. The `playback-restart` event callback applies `seek_absolute()` — avoids `MPV_ERROR_COMMAND (-12)` that fires when mpv is still loading the file.

**State cleared** after successful resume apply (both `_resume_data` and the file).

## Known Quit Bug (fixed)

Pressing `y` on quit modal from PlayerScreen previously triggered `end-file` → `_advance_to_next()` which pushed a new PlayerScreen before the old one was popped. Fix: disconnect `self.player.on_end = None` before `self.player.stop()` in the quit callback.

## Common Mistakes & Troubleshooting

### 1. mpv / python-mpv Issues
**Problem:** `ModuleNotFoundError: No module named 'mpv'`
**Solution:** Run `uv sync` to install `python-mpv`. Ensure mpv binary is on PATH.

### 2. yt-dlp Fails to Fetch Streams
**Problem:** No video results or empty search
**Solution:** Check internet connection and yt-dlp version: `yt-dlp --version`. Update: `uv pip install --upgrade yt-dlp`

### 3. extract_audio_url Fails
**Problem:** "Failed to load audio" with specific error message
**Solution:** The function now returns `(url, error_reason)` tuples. Distinguishes: private video, age-restricted, copyright/removed, network errors.

### 4. Terminal Audio Issues
**Problem:** No sound plays
**Solution:**
- Windows: Ensure default audio output device is selected
- Disable system audio enhancements in device properties
- Check mpv audio device: `mpv --audio-device-list`

### 5. Textual TUI Issues
**Problem:** Application crashes or UI doesn't render
**Solution:** Ensure Textual is installed and Python version is compatible (3.10+)

## Best Practices for AI Agents

### When Modifying Player Logic (player.py)
1. **Use `python-mpv` API** — not subprocess/named pipes. MpvPlayer wraps libmpv.
2. **Set initial volume explicitly** (`player.volume = 50` after creating MPV instance).
3. **Always expose a `process` property** for `if self.player.process` guard checks in the app.
4. **Use `_cleanup()` + lock** for safe teardown. MpvPlayer has a threading lock.
5. **Register property observers** for `time-pos` and `duration`, and `end-file` event callback.
6. **Defer initial seek to `playback-restart`** — `play(path, seek_to=N)` stores `_pending_seek` internally. The `playback-restart` event callback applies `seek_absolute()`. Never call `seek_absolute()` right after `play()`; mpv raises `MPV_ERROR_COMMAND (-12)` when still loading.

### When Modifying Screens (main.py)
1. **Keep screens thin** — UI only. All logic lives on `YouTubePlayerApp`.
2. **Use `self.app` with type: ignore** to access app methods from screens.
3. **Callbacks from player thread** must use `self.call_from_thread()` to update screen widgets.
4. **Check `isinstance(self.screen, PlayerScreen)`** before updating player UI — it may have been popped.
5. **`@work` methods** return `Worker`, not awaitable. Call without `await`.
6. **`push_screen` during playback** is fine — the player keeps playing in the background.
7. **Quit binding** uses `QuitScreen` modal with `push_screen` + callback pattern.
8. **SeekModal (`g` key)** — `push_screen(SeekModal(duration), callback)`. Modal parses H:MM:SS/M:SS/seconds, validates against duration, dismisses with float or None. Callback calls `player.seek_absolute()`. Guard with `isinstance(self.screen, SeekModal)` to prevent re-entry.

### When Using Config (config.py)
1. **Import `CONFIG` singleton** — `from config import CONFIG`
2. **Cache settings live in `CacheConfig`** — max age, cache dir, etc.
3. **Don't hardcode cache paths** — use `CONFIG` values in search.py

### When Modifying Search Logic (search.py)
1. **Use `asyncio.create_subprocess_exec`** for async yt-dlp calls.
2. **`extract_audio_url` returns `(url, error)` tuple** — handle both values.
3. **Error messages** are derived from stderr content. Keep the heuristics updated.
4. **Files named `ytplay-{video_id}.{ext}`** — video_id from URL, not random UUID.
5. **`.done` marker files** track completed downloads; partial files get deleted.
6. **`_cleanup_cache()`** runs before every new download to purge old files.
7. **Drain subprocess pipes in `DownloadHandle.kill()`** — close stdout/stderr streams + `p.wait()` after killing. Prevents "unclosed transport" `ValueError: I/O operation on closed pipe` warnings on Windows (Python 3.14+ raises on pipe fileno after close).

### Important Notes
- **Windows paths:** Use `os.pathsep` for PATH modifications in player.py for DLL loading.
- **Screen stack:** Textual's screen stack is used naturally. `pop_screen()` pops to the previous screen.
- **`current_index = -1`** means "no track playing." The guard `0 <= next_index < len(results)` handles auto-advance correctly from this state.
- **`_advance_to_next`** pops PlayerScreen back to ResultsScreen when queue is exhausted.
- **Git add on Windows:** Match filename case exactly from `git status` output. `git add AGENTS.md` may silently no-op if the real file is `Agents.md`.

## Dependencies (pyproject.toml)
```toml
dependencies = [
    "textual>=8.2.0",
    "yt-dlp>=2024.0.0",
    "python-mpv>=1.0.0",
]
```

## Testing Commands
```bash
# Install dev dependencies
uv sync --dev

# Run all tests
uv run pytest -v

# Run specific test file
uv run pytest test/test_search.py -v

# Run by keyword
uv run pytest -k "extract_video_id"

# Test yt-dlp connectivity
yt-dlp "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Test mpv installation
mpv --version

# Run application
uv run main.py
```

# Record common error here
