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
- `_cleanup()` never blocks on `player.terminate()` directly — it's run on a daemon helper thread with a bounded wait (`TERMINATE_TIMEOUT_SECS`, 5.0s) via `_terminate_with_timeout()`, because libmpv's shutdown can hang (see "Known issue" below). A timeout triggers a full thread-stack dump for diagnostics rather than freezing the app.

### Playback recovery (`main.py`)

`_handle_track_end` distinguishes a genuine end-of-track from a false EOF: if mpv reports a normal end (`error_msg is None`) but the backing `DownloadHandle` isn't done yet, that's treated as recoverable (the file just hasn't grown far enough) rather than "queue advance." Real errors and this false-EOF case both go through `_attempt_recovery()`, which re-extracts/restarts the download and resumes at `max(player.current_time, self._desired_position)`, capped at `MAX_RECOVERY_ATTEMPTS` (3).

### Config (`config.py`)

Single `CONFIG` singleton (`CacheConfig` dataclass) — `cache_dir`, `max_cache_age_hours`, `resume_max_age_days`. Import as `from config import CONFIG`; don't hardcode cache paths elsewhere.

## Windows-specific notes

- `git add <file>` can silently no-op if the on-disk filename's case doesn't match what's in `git status` (e.g. `Agents.md` vs `AGENTS.md`) — match case exactly.
- Path separators / PATH env manipulation should use `os.pathsep`/`os.path.join`, not hardcoded `:`/`/`.
- Python 3.14+ raises when touching a closed pipe's fileno — see `DownloadHandle.kill()` above.
- `mpv-lib/` (vendored `libmpv-2.dll` + MinGW import libs/headers, ~112MB) is **gitignored, not tracked** — it exceeded GitHub's 100MB file limit and was purged from git history on 2026-08-12. It must exist on disk locally for playback to work, but is fetched automatically rather than committed:
  - `setup_mpv.py` (`fetch_mpv_lib()`) downloads the latest plain (non-`-v3`/AVX2) x86_64 build from the community Windows builds linked off mpv.io (hosted on SourceForge, discovered via that project's RSS feed so the exact filename/version isn't hardcoded), and places it at `mpv-lib/`.
  - Extraction is pure Python, no external program shelled out to. `py7zr` parses the 7z container and decompresses the LZMA/LZMA2 substreams, but can't decode the BCJ2 filter these archives apply to the main DLL (a known py7zr gap — see its `UnsupportedCompressionMethodError` for method id `0303011b`). `setup_mpv.py`'s `_bcj2_decode()` implements that one filter from the public-domain 7-Zip SDK algorithm (a 258-context binary range coder deciding, per `CALL`/`JMP`/`Jcc` opcode, whether to splice a reconstructed relative address back into the main stream) — `_resolve_input`/`_resolve_output` walk the folder's coder/bindpair graph generically to feed it the right 4 substreams. Verified bit-for-bit against a reference extraction before relying on it.
  - Download is chunked across `DOWNLOAD_THREADS` (6) threads via HTTP Range requests when the file is large enough (`MIN_CHUNKED_SIZE`) — SourceForge round-robins each request to a different mirror but all mirrors serve identical bytes and honor Range, so this is safe; falls back to a single-threaded download if a chunk request fails for any reason.
  - `main.py` calls `fetch_mpv_lib()` at startup (no-ops if `mpv-lib/libmpv-2.dll` already exists) — a fresh clone just needs `uv run main.py`. Run `uv run setup_mpv.py --force` to re-fetch manually.

## Logging

All modules log via the shared `"yt-play"` logger — not stdout, since stdout is the TUI. **Session-wise log files**: each run creates its own file, `log/yt-play-{YYYYMMDD-HHMMSS}-{pid}.log` (see `SESSION_ID` in `main.py`, set up before `logging.basicConfig`), with a `SESSION START` banner line. Format includes `(%(threadName)s)` — needed because playback callbacks arrive from mpv's own thread and the stall-watchdog thread, not just the UI thread. When debugging playback/download issues, find the log file matching the run's start time rather than adding print statements.

## Known issue: Ctrl+D quit can freeze on the QuitScreen (fix applied 2026-08-12, watch for recurrence)

Reported: after long playback sessions (hours), pressing Ctrl+D shows the "Stop playback and return to results?" QuitScreen, but Y/N/Esc stop being recognized — app appears frozen.

**Original theory (watchdog/`call_from_thread` cross-thread deadlock) was disproven by an actual repro log** (`log/yt-play-20260812-162916-8560.log`). Sequence at the freeze:
```
YES PRESSED
QUIT CALLBACK RESULT=True
BEFORE player.stop()
CLEANUP enter
STOP_WATCHDOG joining thread=mpv-stall-watchdog (timeout=1.0s)
STOP_WATCHDOG thread=mpv-stall-watchdog joined cleanly   <- watchdog join was fine, took 8ms
CLEANUP acquiring lock
CLEANUP lock acquired
[nothing after this — no "Error during mpv terminate", no "CLEANUP done"]
```
The watchdog join (the originally-suspected deadlock point) completed in 8ms — not the cause. The hang is **inside `self._player.terminate()` itself** (`player.py` `_cleanup()`, the line right after "CLEANUP lock acquired"), which never returns and never raises.

**Current theory**: ~40s before the freeze, mpv's own event thread logged:
```
(MPVEventHandlerThread) MPV LOG [warn/file] File is apparently being appended to, will keep retrying with timeouts.
```
at a position ~4658s (77+ min) into a long video whose download was still trickling in. Likely libmpv's internal demuxer/network thread was mid-retry-loop reading the still-growing file when `terminate()` sent mpv's shutdown/quit command; `python-mpv`'s `terminate()` blocks waiting for the core to fully shut down and **has no timeout of its own** — if the demuxer thread doesn't respond to quit promptly (stuck in that retry-with-timeout loop), `terminate()` hangs indefinitely, which reads to the user as "Y/N not recognized" (the whole UI thread is blocked inside the dismiss callback, so no key events get processed at all).

The process did not self-recover — it was gone from the process list by the time this was checked, i.e. had to be force-killed.

**Still open**: why the demuxer thread doesn't unblock — is it stuck on a Windows file-read of the partially-written download file specifically, or something else in libmpv's shutdown path. No timeout exists anywhere in this call chain, so a hang here is a real freeze, not a self-resolving one like the old watchdog-join theory assumed.

**Instrumentation in place** (already added, keep for next repro):
- `player.py` `_cleanup()`: logs before/after `_stop_watchdog()`, before/after acquiring `self._lock` — this is what pinned the hang location to `terminate()`.
- `player.py` `_stop_watchdog()`: logs the `join(timeout=1.0)` call and warns if it times out.
- `player.py` watchdog stall path and `end-file` event callback: log immediately before/after calling `self.on_end(...)`, tagged with thread name.
- `main.py` `_on_track_end`: logs before/after `call_from_thread(...)`, tagged with thread name.

**Fix applied**: `MpvPlayer._cleanup()` now detaches `self._player` immediately (under the lock) and calls `_terminate_with_timeout()`, which runs `player.terminate()` on a daemon helper thread (`mpv-terminate-worker`) and only waits up to `TERMINATE_TIMEOUT_SECS` (5.0s). If it doesn't finish in time, the app logs `TERMINATE TIMED OUT` at `CRITICAL`, dumps every thread's Python stack via `_dump_thread_stacks()` (`faulthandler.dump_traceback(all_threads=True)`), and moves on — the hung mpv thread is abandoned (daemon, so it won't block process exit) instead of freezing the UI. This bounds the freeze to ~5s instead of forever, and the next occurrence's log will show exactly what libmpv's internal threads were doing at the moment of the hang (confirms/denies the "stuck retrying reads on a still-growing file" theory for real).

**Log retention**: session log files are now pruned at startup (`main.py` `_cleanup_old_logs()`, mirrors `search.py`'s `_cleanup_cache()` pattern) — enforces both `CONFIG.log_max_age_days` (14 days) and `CONFIG.log_max_count` (20 files), whichever is stricter. Never touches the current session's own log file.
