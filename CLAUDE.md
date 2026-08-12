# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Terminal-based YouTube audio player. `yt-dlp` for search/stream extraction, `libmpv` (via `python-mpv`) for playback, `textual` for the TUI. Single-package layout — no `src/` dir, four top-level modules: `main.py`, `search.py`, `player.py`, `config.py`.

## Commands

```bash
uv sync                        # install deps
uv sync --dev                  # install + dev deps (pytest)
uv run main.py                 # run the app
uv run pytest -v                        # all tests
uv run pytest test/test_search.py -v    # one test file
uv run pytest -k "extract_video_id"     # by keyword
```

No lint/format config present (no ruff/black/mypy configured) — don't invent one, don't run tools that aren't set up.

## Architecture

### Screen stack (Textual `push_screen`/`pop_screen`)

```
[MenuScreen] --history--> [HistoryScreen] --select--------> [PlayerScreen]
     │                                                            ↑
     ├--search---------> [SearchScreen] --search--> [ResultsScreen] --select--┘
     │
     └--URL-------------> [UrlModal] ------------------------------> [PlayerScreen]
```

All screen/modal classes live in `main.py` (no separate widgets module):
- **MenuScreen** — entry point, three options: Play History / Search / Play from URL.
- **HistoryScreen** — lists play history newest-first (list is reversed for display; index math un-reverses on selection). Selecting plays the entry, downloading if not cached.
- **SearchScreen** — `Input.Submitted` → `app.do_search()`. `$0` repeats the last search and auto-plays the first result.
- **ResultsScreen** — `OptionList` of results; selection calls `app.play_at(index)`.
- **PlayerScreen** — now-playing title, progress bar, live updates from player callbacks.
- **QuitScreen** — `ModalScreen[bool]`, Y/N/Esc confirmation on `Ctrl+D`.
- **SeekModal** — `g` key, parses H:MM:SS / M:SS / seconds, validates against duration, dismisses with float or `None`.
- **UrlModal** — "Play from URL" input, validates via `_extract_video_id` before dismissing.

All app state and logic lives on `YouTubePlayerApp`; screens stay thin (UI + calling `self.app` methods). Bindings are app-level (not per-screen) so playback controls (space/seek/volume/n/p/speed) work from any screen.

`@work` decorators mark async methods that dispatch background jobs and return `Worker` objects — never `await` them:
- `do_search` uses `exclusive=True` (concurrent searches are wasteful).
- `_play_video_async` intentionally does **not** use `exclusive=True`, so N/P (next/prev) works — the previous download is cancelled explicitly via `_cleanup_active_download()` inside the method, not by worker exclusivity.
- Callbacks fired from the player's own thread (`on_time_update`, `on_error`, `on_end`) must cross back via `self.call_from_thread(...)` before touching any widget.

### Download-to-disk-and-play (`search.py`)

Streaming directly from YouTube's CDN URL works for in-order playback but seeking ahead of mpv's network buffer can stall indefinitely. Instead, yt-dlp downloads audio to a local file in the background and mpv plays the file while it's still growing — local seeks are instant once bytes are on disk; seeking ahead of what's downloaded just waits for the download to catch up.

- `AUDIO_FORMAT_SELECTOR` prefers a **progressive** (non-DASH-fragmented) HTTPS stream — DASH segments handle arbitrary seeks less reliably.
- No `--extract-audio`/`--audio-format` — that triggers an ffmpeg post-process that only writes output after the *entire* download finishes, defeating play-while-downloading.
- Files named `ytplay-{video_id}.{ext}` in `data/`; a `.done` marker file marks a completed download.
- `start_audio_download()`: cache hit (marker + file both exist) → return handle with `is_cached=True`, no subprocess. Partial download (file present, no marker) → deleted and re-downloaded.
- `_cleanup_cache()` runs before every new download: removes orphan `.done` markers, files not referenced by any resume-history `video_id`, and history-referenced files older than `CacheConfig.max_cache_age_hours` (7 days).
- `DownloadHandle.kill()` closes stdout/stderr and calls `p.wait()` after killing — needed to avoid `ValueError: I/O operation on closed pipe` on Windows (Python 3.14+ raises if a pipe fileno is touched after close).
- `extract_audio_url` / `fetch_video_title` return errors as tuples/fallback strings rather than raising — callers must handle the failure value, not assume success.

### Resume / play history

Position is saved to `data/resume_state.json` as a list of per-video entries (keyed by `video_id`, upserted in place), on quit from `PlayerScreen` and every 5s during playback.

- **Resume is a per-video history lookup, not "the last played candidate."** `_play_video_async()` calls `_lookup_history_position(video_id)` whenever `seek_to == 0.0` (the default for search-result and URL playback). This works for *any* previously-watched video regardless of its position in the history array — critical because `_save_resume_data` upserts in place (doesn't move the entry to the end), so `data[-1]` is *not* reliably "most recent."
- `_lookup_history_position` returns `None` if `position >= duration - 5` (near-finished videos start over instead of resuming at EOF).
- `play_history_entry` passes an explicit `seek_to=position`, bypassing the lookup (position is already known).
- History is stateless on disk — read fresh on every lookup, no in-memory resume candidate cached at startup.
- `_load_and_prune_history()` (app init) migrates the legacy single-dict format to a list and prunes entries older than `CONFIG.resume_max_age_days` (30 days).

### Player (`player.py`)

`MpvPlayer` wraps `python-mpv` (libmpv bindings, not a subprocess/named-pipe player). Key points for anyone touching it:

- `os.environ["PATH"]` is prepended with `mpv-lib/` at import time so libmpv's DLL (`mpv-2.dll`) is discoverable on Windows — don't remove this before importing `mpv`.
- **Initial seek is deferred**: `play(path, seek_to=N)` stores `N` in `_pending_seek`; the actual `seek_absolute()` call happens inside the `playback-restart` event callback. Calling `seek_absolute()` immediately after `play()` raises `MPV_ERROR_COMMAND (-12)` because mpv is still loading.
- **Stall watchdog**: a background thread polls `time-pos`; if playback hasn't advanced for `STALL_TIMEOUT_SECS` (6s) while unpaused and not buffering, and it's been at least `SEEK_SETTLE_SECS` (1s) since the last seek, it fires `on_end("Playback stalled...")` to trigger recovery. This exists because a seek past the end of the demuxer cache can silently stop advancing without mpv reporting an error.
- Expose `process` as a bool property (`self._player is not None`) — callers guard on `if self.player.process`.
- `_cleanup()` uses a threading lock; always goes through `stop()`, never touches `self._player` directly from outside.

### Playback recovery (`main.py`)

`_handle_track_end` distinguishes a genuine end-of-track from a false EOF: if mpv reports a normal end (`error_msg is None`) but the backing `DownloadHandle` isn't done yet, that's treated as recoverable (the file just hasn't grown far enough) rather than "queue advance." Real errors and this false-EOF case both go through `_attempt_recovery()`, which re-extracts/restarts the download and resumes at `max(player.current_time, self._desired_position)`, capped at `MAX_RECOVERY_ATTEMPTS` (3).

### Config (`config.py`)

Single `CONFIG` singleton (`CacheConfig` dataclass) — `cache_dir`, `max_cache_age_hours`, `resume_max_age_days`. Import as `from config import CONFIG`; don't hardcode cache paths elsewhere.

## Windows-specific notes

- `git add <file>` can silently no-op if the on-disk filename's case doesn't match what's in `git status` (e.g. `Agents.md` vs `AGENTS.md`) — match case exactly.
- Path separators / PATH env manipulation should use `os.pathsep`/`os.path.join`, not hardcoded `:`/`/`.
- Python 3.14+ raises when touching a closed pipe's fileno — see `DownloadHandle.kill()` above.

## Logging

All modules log to `log/yt-play.log` (created at import time by `main.py`) via the shared `"yt-play"` logger — not stdout, since stdout is the TUI. When debugging playback/download issues, check this file rather than adding print statements.
