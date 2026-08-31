import asyncio
import glob
import json
import logging
import os
import threading
import time
from typing import Optional
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Header, Footer, Input, OptionList, Label, ProgressBar
from textual.widgets.option_list import Option
from textual.binding import Binding
from textual import work

from search import search_youtube, start_audio_download, wait_for_file_growth, DownloadHandle, _extract_video_id
from config import CONFIG

LOG_DIR = "log"
os.makedirs(LOG_DIR, exist_ok=True)
# One log file per run (session), named by start time + PID, so overlapping
# threads (UI thread vs mpv watchdog thread) can be traced within a single
# run without interleaving across separate app launches.
SESSION_ID = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
CURRENT_LOG_FILE = os.path.join(LOG_DIR, f"yt-play-{SESSION_ID}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(threadName)s) %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(CURRENT_LOG_FILE),
    ],
)
log = logging.getLogger("yt-play")
log.info("SESSION START id=%s pid=%d", SESSION_ID, os.getpid())


def _cleanup_old_logs() -> None:
    """Delete old session log files, enforcing both limits: age
    (CONFIG.log_max_age_days) and count (CONFIG.log_max_count), whichever
    is stricter. Never touches the current session's own log file.
    """
    try:
        files = [p for p in glob.glob(os.path.join(LOG_DIR, "yt-play-*.log")) if p != CURRENT_LOG_FILE]
    except OSError:
        log.exception("LOG cleanup failed to list %s", LOG_DIR)
        return

    now = time.time()
    age_cutoff = now - CONFIG.log_max_age_days * 86400
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)  # newest first

    removed = 0
    # Keep at most log_max_count files total, counting the current session's
    # file as one of them.
    keep_count = max(0, CONFIG.log_max_count - 1)
    for i, fpath in enumerate(files):
        try:
            mtime = os.path.getmtime(fpath)
        except OSError:
            continue
        too_old = mtime < age_cutoff
        over_count = i >= keep_count
        if not (too_old or over_count):
            continue
        try:
            os.remove(fpath)
            removed += 1
            log.info("LOG cleanup removed %s (%s)", fpath, "expired" if too_old else "over count limit")
        except OSError:
            log.exception("LOG cleanup failed to remove %s", fpath)
    if removed:
        log.info("LOG cleanup removed %d old session log(s)", removed)


_cleanup_old_logs()

# mpv-lib/ (libmpv DLL) is gitignored — too large for GitHub — so pull it on
# first run if it's missing. See setup_mpv.py / CLAUDE.md "Windows-specific
# notes". Must happen before `import player`, which imports `mpv` and needs
# the DLL on PATH immediately.
from setup_mpv import fetch_mpv_lib
try:
    fetch_mpv_lib()
except Exception:
    log.exception("Failed to auto-fetch mpv-lib/ — run `uv run setup_mpv.py` manually")
    raise

from player import MpvPlayer

# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class SearchInput(Input):
    BINDINGS = [
        # Unbind ctrl+d so it bubbles up to app-level quit.
        Binding("ctrl+d", "", "", show=False, priority=True),
    ]


# ---------------------------------------------------------------------------
# Modal
# ---------------------------------------------------------------------------

class QuitScreen(ModalScreen[bool]):
    """Keyboard-driven confirmation. Y / N / Escape."""
    BINDINGS = [
        ("y", "yes", "Yes"),
        ("n", "no", "No"),
        ("escape", "no", "Cancel"),
    ]
    def __init__(self, prompt: str = "Quit YouTube Player? (y/n)") -> None:
        super().__init__()
        self._prompt = prompt
    def compose(self) -> ComposeResult:
        yield Label(self._prompt)
    def action_yes(self) -> None:
        log.info("YES PRESSED")
        self.dismiss(True)
    def action_no(self) -> None:
        log.info("NO PRESSED")
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

class MenuScreen(Screen):
    """Entry point: choose Play History or Search."""

    CSS = """
    MenuScreen { align: center middle; }
    MenuScreen Vertical { width: 40; height: auto; margin: 1; }
    #menu_title { text-align: center; text-style: bold; padding-bottom: 1; }
    #menu_list { margin-top: 1; height: 5; }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Label("YouTube Player", id="menu_title")
            yield OptionList(id="menu_list")
        yield Footer()

    def on_mount(self) -> None:
        menu = self.query_one("#menu_list", OptionList)
        menu.add_option(Option("▶  Play History", id="menu_history"))
        menu.add_option(Option("🔍  Search", id="menu_search"))
        menu.add_option(Option("🔗  Play from URL", id="menu_url"))
        menu.focus()

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        app: YouTubePlayerApp = self.app  # type: ignore
        if event.option_id == "menu_history":
            app.go_to_history()
        elif event.option_id == "menu_search":
            app.go_to_search()
        elif event.option_id == "menu_url":
            app.action_play_from_url()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.exit()


class HistoryScreen(Screen):
    """Show play history; select to play (downloads if not cached)."""

    CSS = """
    HistoryScreen { layout: vertical; }
    #history_title { padding: 0 1; }
    #history_list { height: 1fr; }
    #history_empty { padding: 1; text-align: center; text-style: dim; }
    #history_help { padding: 0 1; text-style: dim; }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Play History", id="history_title")
        yield OptionList(id="history_list")
        yield Label("No play history yet", id="history_empty")
        yield Label("[Esc] Back  —  [D]elete selected entry", id="history_help", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._populate_list()

    def _populate_list(self, highlight_index: Optional[int] = None) -> None:
        app: YouTubePlayerApp = self.app  # type: ignore
        history = app._read_history()
        option_list = self.query_one("#history_list", OptionList)
        empty_label = self.query_one("#history_empty", Label)
        option_list.clear_options()
        if not history:
            option_list.display = False
            empty_label.display = True
            return
        empty_label.display = False
        option_list.display = True
        # Newest first
        for i, entry in enumerate(reversed(history)):
            title = entry.get("title", "Unknown")
            position = entry.get("position", 0.0)
            label = f"{title}"
            if position > 0:
                label += f"  [{PlayerScreen._fmt(position)}]"
            option_list.add_option(Option(label, id=f"hist_{i}"))
        option_list.focus()
        if highlight_index is not None and option_list.option_count:
            option_list.highlighted = min(highlight_index, option_list.option_count - 1)

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        app: YouTubePlayerApp = self.app  # type: ignore
        app.play_history_entry(event.option_index)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.pop_screen()
        elif event.key in ("d", "delete"):
            self._delete_selected()

    def _delete_selected(self) -> None:
        option_list = self.query_one("#history_list", OptionList)
        index = option_list.highlighted
        if index is None:
            return
        app: YouTubePlayerApp = self.app  # type: ignore
        history = app._read_history()
        reversed_history = list(reversed(history))
        if index >= len(reversed_history):
            return
        title = reversed_history[index].get("title", "Unknown")

        def _cb(confirmed: bool) -> None:
            if not confirmed:
                return
            deleted_title = app.delete_history_entry(index)
            if deleted_title:
                app.notify(f"Deleted: {deleted_title}", timeout=2)
            self._populate_list(highlight_index=index)

        self.app.push_screen(QuitScreen(f"Delete '{title}' from history? (y/n)"), _cb)


class SearchScreen(Screen):
    CSS = """
    SearchScreen { align: center middle; }
    SearchScreen > Vertical { width: 60; height: auto; }
    Input { margin-bottom: 1; }
    Label { text-align: center; }
    #recent_searches { margin-top: 1; height: auto; }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield SearchInput(placeholder="Search YouTube... ($0 = repeat last)", id="search_input")
            yield Label("Press Enter to search", id="search_status")
            yield Label("", id="recent_searches")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Input).focus()
        self._refresh_recent()

    def on_screen_resume(self) -> None:
        self.query_one("#search_status", Label).update("Press Enter to search")
        self.query_one(Input).focus()
        self._refresh_recent()

    def _refresh_recent(self) -> None:
        app: YouTubePlayerApp = self.app  # type: ignore
        if app.recent_searches:
            lines = ["[bold]Recent:[/]"] + [f"  • {q}" for q in app.recent_searches]
            self.query_one("#recent_searches", Label).update("\n".join(lines))
        else:
            self.query_one("#recent_searches", Label).update("")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search_input":
            return
        value = event.input.value.strip()
        if not value:
            return
        app: YouTubePlayerApp = self.app  # type: ignore
        if value == "$0":
            if not app.recent_searches:
                self.query_one("#search_status", Label).update("No recent search to repeat")
                return
            self.query_one("#search_status", Label).update("Repeating last search...")
            app.do_search(app.recent_searches[0], auto_play_first=True)
            return
        self.query_one("#search_status", Label).update("Searching...")
        app.do_search(value)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.pop_screen()


class ResultsScreen(Screen):
    CSS = """
    ResultsScreen { layout: vertical; }
    #results_title { padding: 0 1; }
    #results_list { height: 1fr; }
    #results_status { padding: 0 1; text-style: italic; color: $warning; }
    #results_help { padding: 0 1; text-style: dim; }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Search Results", id="results_title")
        yield OptionList(id="results_list")
        yield Label("", id="results_status")
        yield Label(
            "[Esc] Back to search  —  [N]ext  [P]rev  available during playback  —  "
            "[PgDn] Next page  [PgUp] Prev page",
            id="results_help",
            markup=False,
        )
        yield Footer()

    def on_mount(self) -> None:
        self._populate()

    def _populate(self) -> None:
        app: YouTubePlayerApp = self.app  # type: ignore
        results = app.results
        title = self.query_one("#results_title", Label)
        option_list = self.query_one("#results_list", OptionList)
        self.query_one("#results_status", Label).update("")
        title.update(f"Search Results ({len(results)}) — Page {app.search_page}")
        option_list.clear_options()
        for i, res in enumerate(results):
            text = f"{res.title} [{res.duration_str}] - {res.uploader}"
            option_list.add_option(Option(text, id=f"result_{i}"))
        self.query_one("#results_list").focus()

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "results_list":
            app: YouTubePlayerApp = self.app  # type: ignore
            app.play_at(event.option_index)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.pop_screen()
        elif event.key == "pagedown":
            self.query_one("#results_status", Label).update("Loading...")
            self.app.next_search_page()  # type: ignore
        elif event.key == "pageup":
            self.query_one("#results_status", Label).update("Loading...")
            self.app.prev_search_page()  # type: ignore


class SeekModal(ModalScreen[float]):
    """Input modal to seek to a specific timestamp. Dismisses with seconds or None."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, duration: float) -> None:
        super().__init__()
        self._duration = duration
        self._max_str = PlayerScreen._fmt(duration)

    def compose(self) -> ComposeResult:
        yield Label(f"Seek to position (max [bold]{self._max_str}[/]):", id="seek_label")
        yield Input(placeholder="e.g. 1:30:40, 5:30, 90", id="seek_input")
        yield Label("", id="seek_error")

    def on_mount(self) -> None:
        self.query_one("#seek_input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.input.value.strip()
        seconds = self._parse_timestamp(raw)
        if seconds is None:
            self.query_one("#seek_error", Label).update(
                f"[red]Invalid format. Use H:MM:SS, M:SS, or plain seconds.[/]"
            )
            return
        if seconds < 0:
            seconds = 0
        if seconds > self._duration:
            self.query_one("#seek_error", Label).update(
                f"[red]Position {PlayerScreen._fmt(seconds)} exceeds duration ({self._max_str}). "
                f"Max is {PlayerScreen._fmt(self._duration)}.[/]"
            )
            return
        self.dismiss(seconds)

    @staticmethod
    def _parse_timestamp(raw: str) -> Optional[float]:
        """Parse H:MM:SS, M:SS, or plain seconds. Returns None on failure."""
        raw = raw.strip()
        if not raw:
            return None
        parts = raw.split(":")
        try:
            if len(parts) == 3:
                h, m, s = parts
                return int(h) * 3600 + int(m) * 60 + float(s)
            elif len(parts) == 2:
                m, s = parts
                return int(m) * 60 + float(s)
            elif len(parts) == 1:
                return float(parts[0])
        except ValueError:
            pass
        return None


class UrlModal(ModalScreen[str]):
    """Input modal for a YouTube URL. Dismisses with the URL string or None."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    CSS = """
    UrlModal { align: center middle; }
    #url_dialog { width: 60; padding: 1 2; border: thick $primary; background: $surface; }
    #url_label { text-align: center; padding-bottom: 1; }
    #url_error { text-align: center; padding-top: 1; height: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="url_dialog"):
            yield Label("Enter YouTube URL:", id="url_label")
            yield Input(placeholder="https://www.youtube.com/watch?v=...", id="url_input")
            yield Label("", id="url_error")

    def on_mount(self) -> None:
        self.query_one("#url_input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        url = event.input.value.strip()
        if not url:
            return
        from search import _extract_video_id
        vid = _extract_video_id(url)
        if not vid:
            self.query_one("#url_error", Label).update(
                "[red]Invalid YouTube URL — need video ID[/]"
            )
            return
        self.dismiss(url)


class PlayerScreen(Screen):
    CSS = """
    PlayerScreen { layout: vertical; }
    #now_playing { padding: 1; text-align: center; }
    #progress_container { height: auto; align: center middle; margin: 0 2; }
    #time_current, #time_total { width: 8; text-align: center; }
    ProgressBar { width: 1fr; margin: 0 1; }
    #controls_help { padding: 0 1; text-align: center; text-style: dim; }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Now Playing: Nothing", id="now_playing")
        with Horizontal(id="progress_container"):
            yield Label("00:00", id="time_current")
            yield ProgressBar(total=100, show_eta=False, id="progress_bar")
            yield Label("00:00", id="time_total")
        yield Label(
            "[Space] Play/Pause  [Left/Right] Seek ±5s  [Up/Down] Vol ±5  "
            "[G]o to position  [N]ext  [P]rev  [/] Speed ∓0.25  [Esc] Back  [Ctrl+D] Quit",
            id="controls_help",
            markup=False,
        )
        yield Footer()

    def on_mount(self) -> None:
        app: YouTubePlayerApp = self.app  # type: ignore
        self._update_now_playing(app.current_title)

    def update_now_playing(self, title: str) -> None:
        self._update_now_playing(title)

    def _update_now_playing(self, title: str) -> None:
        label = self.query_one("#now_playing", Label)
        if title:
            label.update(f"Now Playing: {title}")
        else:
            label.update("Now Playing: Nothing")

    def update_progress(self, current_time: float, duration: float) -> None:
        """Called from the player callback – always on the main thread."""
        if duration > 0:
            self.query_one(ProgressBar).update(progress=(current_time / duration) * 100)
        self.query_one("#time_current", Label).update(self._fmt(current_time))
        self.query_one("#time_total", Label).update(self._fmt(duration))

    @staticmethod
    def _fmt(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.pop_screen()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class YouTubePlayerApp(App):
    CSS = """
    Screen { layout: vertical; }
    """

    BINDINGS = [
        ("ctrl+d", "quit", "Quit"),
        ("space", "toggle_pause", "Play/Pause"),
        ("right", "seek_forward", "Seek +5s"),
        ("left", "seek_backward", "Seek -5s"),
        ("up", "volume_up", "Vol +5"),
        ("down", "volume_down", "Vol -5"),
        ("g", "go_to_position", "Go to position"),
        ("n", "next_track", "Next"),
        ("p", "prev_track", "Prev"),
        ("]", "speed_up", "Speed +"),
        ("[", "speed_down", "Speed -"),
    ]

    def __init__(self):
        super().__init__()
        self.player = MpvPlayer()
        self.player.on_time_update = self._on_time_update
        self.player.on_error = self._on_player_error
        self.player.on_end = self._on_track_end

        self.results: list = []
        self.current_index: int = -1
        self.current_title: str = ""
        self.current_youtube_url: str = ""

        self.search_query: str = ""
        self.search_page: int = 1
        # yt-dlp's --dump-json fully extracts metadata per video (~3s/video) -
        # fetching in big batches just multiplies the wait linearly, no economy
        # of scale. So fetch one page (10) at a time, and prefetch the next
        # page in the background so PgDn often finds it already cached.
        self.SEARCH_PAGE_SIZE: int = 10
        self._search_cache: list = []
        self._search_exhausted: bool = False
        self._search_cache_lock = asyncio.Lock()

        self._recovery_attempts: int = 0
        self.MAX_RECOVERY_ATTEMPTS: int = 3
        self._desired_position: float = 0.0

        self._active_download: Optional[DownloadHandle] = None

        self._resume_path = os.path.join("data", "resume_state.json")
        self._load_and_prune_history()
        self._last_pos_save: float = 0.0

        self.recent_searches: list = []  # max 10, newest first

    # -- Navigation -------------------------------------------------------

    def on_mount(self) -> None:
        self.push_screen(MenuScreen())

    def go_to_history(self) -> None:
        self.push_screen(HistoryScreen())

    def go_to_search(self) -> None:
        self.push_screen(SearchScreen())

    def play_history_entry(self, index: int) -> None:
        """Play from history: newest=0. If cached, play directly; else download."""
        history = self._read_history()
        if not history:
            self.notify("No history entries", timeout=2)
            return
        # history list reversed in screen — reverse back
        reversed_history = list(reversed(history))
        if index < 0 or index >= len(reversed_history):
            return
        entry = reversed_history[index]
        video_id = entry.get("video_id")
        title = entry.get("title", "Unknown")
        url = entry.get("url", "")
        position = entry.get("position", 0.0)

        if not url:
            self.notify("No URL in history entry", title="Error", severity="error")
            return

        from search import _check_cache, _extract_video_id
        vid = video_id or _extract_video_id(url)

        # Check if cached — play directly
        if vid:
            cached = _check_cache(vid)
            if cached:
                log.info("HISTORY play: cached hit for %s (%s)", vid, title)
                self.current_index = -1
                self.current_title = title
                self.current_youtube_url = url
                self._recovery_attempts = 0
                self._desired_position = position
                self._cleanup_active_download()
                self.push_screen(PlayerScreen())
                self._play_video_async(url, title, seek_to=position)
                return

        # Not cached — treat like search result (downloads)
        if url:
            log.info("HISTORY play: downloading %s (%s)", vid, title)
            self.results = []  # clear search results
            self.current_index = 0
            self.current_title = title
            self.current_youtube_url = url
            self._recovery_attempts = 0
            self._desired_position = position
            self.push_screen(PlayerScreen())
            self._play_video_async(url, title, seek_to=position)

    def action_play_from_url(self) -> None:
        self.push_screen(UrlModal(), self._on_url_dismiss)

    def _on_url_dismiss(self, url: Optional[str]) -> None:
        if url is None:
            return
        self._play_url_entry(url)

    @work
    async def _play_url_entry(self, url: str) -> None:
        """Fetch title from URL, then start playback (downloads if not cached)."""
        from search import fetch_video_title
        title = await fetch_video_title(url)

        self.results = []
        self.current_index = 0
        self.current_title = title
        self.current_youtube_url = url
        self._recovery_attempts = 0
        self._desired_position = 0.0

        if not isinstance(self.screen, PlayerScreen):
            self.push_screen(PlayerScreen())
        self._play_video_async(url, title)

    # -- Resume / play history -------------------------------------------

    MAX_HISTORY = 200

    def _load_and_prune_history(self) -> None:
        """Migrate legacy resume format and prune entries older than the max age."""
        try:
            if not os.path.exists(self._resume_path):
                return
            with open(self._resume_path) as f:
                data = json.load(f)
            # Old format — single dict → migrate to array
            if isinstance(data, dict):
                data = [data]
                self._write_history(data)
            if isinstance(data, list) and data:
                cutoff = time.time() - CONFIG.resume_max_age_days * 86400
                pruned = [e for e in data if e.get("saved_at", 0) >= cutoff]
                if len(pruned) < len(data):
                    self._write_history(pruned)
                    log.info("HISTORY pruned %d old entries (max_age=%.0fd)", len(data) - len(pruned), CONFIG.resume_max_age_days)
        except Exception:
            log.exception("Failed to load/prune history")

    def _lookup_history_position(self, video_id: str) -> Optional[float]:
        """Return the saved playback position for *video_id*, or None.

        Returns None if the video isn't in history, has no saved position,
        or was essentially finished (position within 5s of duration) so we
        start over instead of resuming at the very end.
        """
        if not video_id:
            return None
        for entry in self._read_history():
            if entry.get("video_id") == video_id:
                position = float(entry.get("position", 0.0) or 0.0)
                duration = float(entry.get("duration", 0.0) or 0.0)
                if position <= 0:
                    return None
                if duration > 0 and position >= duration - 5:
                    return None
                return position
        return None

    def _read_history(self) -> list:
        """Read full history array from disk."""
        try:
            if os.path.exists(self._resume_path):
                with open(self._resume_path) as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return [data]
        except Exception:
            log.exception("Failed to read history")
        return []

    def _write_history(self, entries: list) -> None:
        try:
            with open(self._resume_path, "w") as f:
                json.dump(entries, f, indent=2)
        except Exception:
            log.exception("Failed to write history")

    def delete_history_entry(self, index: int) -> Optional[str]:
        """Delete history entry at *index* (newest=0, matching HistoryScreen's
        reversed display order). Returns the deleted entry's title, or None
        if the index was out of range."""
        history = self._read_history()
        orig_index = len(history) - 1 - index
        if orig_index < 0 or orig_index >= len(history):
            return None
        entry = history.pop(orig_index)
        self._write_history(history)
        log.info("HISTORY deleted: %s (%d entries remain)", entry.get("video_id"), len(history))
        return entry.get("title", "Unknown")

    def _save_resume_data(self) -> None:
        """Upsert current position into play history — one entry per video_id."""
        if not self.current_youtube_url:
            return
        vid = _extract_video_id(self.current_youtube_url)
        if not vid:
            return
        entry = {
            "video_id": vid,
            "title": self.current_title,
            "url": self.current_youtube_url,
            "position": self._desired_position,
            "duration": self.player.duration,
            "saved_at": time.time(),
        }
        history = self._read_history()
        # Remove existing entry for same video_id so re-plays move to the end.
        history = [e for e in history if e.get("video_id") != vid]
        history.append(entry)
        # Trim oldest if over limit
        if len(history) > self.MAX_HISTORY:
            history = history[-self.MAX_HISTORY:]
        self._write_history(history)
        log.info("HISTORY upserted: %s at %.1fs (%d entries)", vid, self._desired_position, len(history))

    # -- Navigation -------------------------------------------------------

    def action_quit(self) -> None:
        if isinstance(self.screen, PlayerScreen):
            def _cb(result: bool) -> None:
                log.info("QUIT CALLBACK RESULT=%s", result)
                log.info("CURRENT SCREEN=%s", type(self.screen).__name__)

                if result:
                    log.info("BEFORE player.stop()")
                    self._save_resume_data()
                    # Disconnect on_end to prevent stop() → end-file →
                    # _advance_to_next from pushing a new PlayerScreen
                    # before we've popped the old one.
                    self.player.on_end = None
                    self.player.stop()
                    log.info("AFTER player.stop()")

                    log.info("BEFORE cleanup")
                    self._cleanup_active_download()
                    log.info("AFTER cleanup")

                    while isinstance(self.screen, PlayerScreen):
                        log.info("POPPING PlayerScreen")
                        self.pop_screen()

                    log.info("DONE")
            self.push_screen(QuitScreen("Stop playback and return to results? (y/n)"), _cb)
            return

        self.player.stop()
        self._cleanup_active_download()
        self.exit()

    # -- Search -----------------------------------------------------------

    @work(exclusive=True)
    async def do_search(self, query: str, auto_play_first: bool = False) -> None:
        log.info("Searching: %s (auto_play=%s)", query, auto_play_first)
        self.search_query = query
        self.search_page = 1
        self._search_cache = []
        self._search_exhausted = False
        await self._ensure_search_cache(self.SEARCH_PAGE_SIZE)
        self.results = self._search_cache[: self.SEARCH_PAGE_SIZE]
        log.info("Search returned %d results, showing page 1", len(self.results))
        self.current_index = -1
        self.current_title = ""
        self.current_youtube_url = ""
        self._recovery_attempts = 0
        # Track recent searches (max 10, deduplicate)
        if query in self.recent_searches:
            self.recent_searches.remove(query)
        self.recent_searches.insert(0, query)
        if len(self.recent_searches) > 10:
            self.recent_searches = self.recent_searches[:10]
        if auto_play_first and self.results:
            self.play_at(0)
        else:
            self.push_screen(ResultsScreen())
        self._prefetch_next_page()

    @work(exclusive=True)
    async def next_search_page(self) -> None:
        target_page = self.search_page + 1
        end = target_page * self.SEARCH_PAGE_SIZE
        await self._ensure_search_cache(end)
        start = (target_page - 1) * self.SEARCH_PAGE_SIZE
        if start >= len(self._search_cache):
            self.notify("No more results", timeout=2)
            self._clear_results_loading()
            return
        self.search_page = target_page
        self.results = self._search_cache[start:end]
        self.current_index = -1
        self._refresh_results_screen()
        self._prefetch_next_page()

    @work(exclusive=True)
    async def prev_search_page(self) -> None:
        if self.search_page <= 1:
            self.notify("Already on first page", timeout=2)
            self._clear_results_loading()
            return
        self.search_page -= 1
        start = (self.search_page - 1) * self.SEARCH_PAGE_SIZE
        end = self.search_page * self.SEARCH_PAGE_SIZE
        self.results = self._search_cache[start:end]
        self.current_index = -1
        self._refresh_results_screen()

    @work(exclusive=True, group="search_prefetch")
    async def _prefetch_next_page(self) -> None:
        """Fetch the page after the one just shown, in the background, so a
        later PgDn often finds it already cached instead of waiting on
        yt-dlp (~3s/video - see SEARCH_PAGE_SIZE comment above)."""
        await self._ensure_search_cache((self.search_page + 1) * self.SEARCH_PAGE_SIZE)

    async def _ensure_search_cache(self, min_len: int) -> None:
        """Fetch further SEARCH_PAGE_SIZE-sized pages until the cache covers
        *min_len* items or the search is exhausted. Lock-guarded so an
        explicit page turn and the background prefetch never double-fetch
        the same page."""
        async with self._search_cache_lock:
            while len(self._search_cache) < min_len and not self._search_exhausted:
                fetch_page = len(self._search_cache) // self.SEARCH_PAGE_SIZE + 1
                log.info("Fetching search page %d for: %s", fetch_page, self.search_query)
                batch = await search_youtube(self.search_query, page=fetch_page, page_size=self.SEARCH_PAGE_SIZE)
                if not batch:
                    self._search_exhausted = True
                    break
                self._search_cache.extend(batch)
                if len(batch) < self.SEARCH_PAGE_SIZE:
                    self._search_exhausted = True

    def _refresh_results_screen(self) -> None:
        if isinstance(self.screen, ResultsScreen):
            self.screen._populate()

    def _clear_results_loading(self) -> None:
        if isinstance(self.screen, ResultsScreen):
            self.screen.query_one("#results_status", Label).update("")

    # -- Playback ---------------------------------------------------------

    def play_at(self, index: int) -> None:
        log.info(
            "PLAY_AT entered: index=%s current_screen=%s",
            index,
            type(self.screen).__name__,
        )
        if index < 0 or index >= len(self.results):
            log.warning(
                "PLAY_AT invalid index=%s results=%s",
                index,
                len(self.results),
            )
            log.warning("play_at: invalid index %d (results: %d)", index, len(self.results))
            return
        self.current_index = index
        result = self.results[index]
        log.info(
            "PLAY_AT selected title=%s",
            result.title,
        )
        self.current_title = result.title
        self.current_youtube_url = result.url
        log.info("Playing [%d/%d]: %s (%s)", index + 1, len(self.results), result.title, result.url)
        self._recovery_attempts = 0
        self._desired_position = 0.0
        log.info("PLAY_AT starting download/play task")
        self._play_video_async(result.url, result.title)
        log.info("PLAY_AT pushing PlayerScreen")
        self.push_screen(PlayerScreen())

    @work
    async def _play_video_async(self, url: str, title: str, seek_to: float = 0.0) -> None:
        log.info("PLAY_VIDEO_ASYNC start: url=%s title=%s seek_to=%.1f", url, title, seek_to)

        # Resume from saved position if this video has a history entry.
        if seek_to == 0.0:
            vid = _extract_video_id(url)
            if vid:
                saved = self._lookup_history_position(vid)
                if saved and saved > 0:
                    seek_to = saved
                    self.notify(f"Resumed at {PlayerScreen._fmt(seek_to)}", timeout=3)
                    log.info("RESUME applied: %s at %.1fs", vid, seek_to)

        # Clean up any previous download before starting a new one.
        self._cleanup_active_download()

        handle, err = await start_audio_download(url)
        if not handle:
            log.error("PLAY_VIDEO_ASYNC failed to start download: %s", err)
            self.notify(err or "Failed to start download", title="Error", severity="error")
            if isinstance(self.screen, PlayerScreen):
                self.pop_screen()
            return

        self._active_download = handle

        if not handle.file_path:
            log.error("PLAY_VIDEO_ASYNC download file path never appeared on disk")
            self.notify("Failed to locate downloaded file", title="Error", severity="error")
            self._cleanup_active_download()
            if isinstance(self.screen, PlayerScreen):
                self.pop_screen()
            return

        # Seeking ahead of what's downloaded only works once the file actually
        # has bytes covering that timestamp — a flat 64KB floor is fine for a
        # fresh start (seek_to=0) but far too small when resuming/recovering
        # deep into a track, since mpv then seeks past the current EOF of the
        # still-growing file and reports a (false) normal end-of-file instead
        # of waiting, which used to get misread as the track finishing.
        min_bytes = 65536
        buffer_timeout = 15.0
        if seek_to > 0:
            min_bytes = max(min_bytes, int(seek_to * 20000))  # ~160kbps conservative estimate
            buffer_timeout = max(buffer_timeout, seek_to * 0.05)  # assume >=20x realtime download speed

        if handle.is_cached:
            # Cached file is already complete — no need to wait for it to grow.
            # The min_bytes estimate can exceed the file's actual size (it's a
            # conservative bitrate guess), which would otherwise make a complete
            # cached file look like it never buffered enough and stall forever.
            got_data = os.path.exists(handle.file_path)
        else:
            log.info(
                "PLAY_VIDEO_ASYNC waiting for initial buffer (min_bytes=%d timeout=%.1f) at %s",
                min_bytes, buffer_timeout, handle.file_path,
            )
            got_data = await wait_for_file_growth(handle.file_path, min_bytes=min_bytes, timeout=buffer_timeout)

        if handle.error:
            log.error("PLAY_VIDEO_ASYNC download failed before playable: %s", handle.error)
            self.notify(handle.error, title="Download Error", severity="error")
            if isinstance(self.screen, PlayerScreen):
                self.pop_screen()
            return

        if not got_data:
            log.error("PLAY_VIDEO_ASYNC timed out waiting for download to produce data")
            self.notify("Timed out starting download", title="Error", severity="error")
            if isinstance(self.screen, PlayerScreen):
                self.pop_screen()
            return

        log.info(
            "PLAY_VIDEO_ASYNC starting mpv on local file (seek_to=%.1f): %s",
            seek_to, handle.file_path,
        )
        self.player.play(handle.file_path, seek_to=seek_to)
        player_screen = self.screen
        if isinstance(player_screen, PlayerScreen):
            player_screen.update_now_playing(title)

        # Let the download finish in the background; log its outcome.
        await handle.wait()
        if handle.error and handle is self._active_download:
            log.error("PLAY_VIDEO_ASYNC background download ended with error: %s", handle.error)

    def _cleanup_active_download(self) -> None:
        """Kill any in-progress download and remove its temp file."""
        handle = self._active_download
        self._active_download = None
        if not handle or handle.is_cached:
            return
        handle.kill()
        if not handle.file_path:
            return
        try:
            if os.path.exists(handle.file_path):
                os.remove(handle.file_path)
                log.info("CLEANUP removed temp file: %s", handle.file_path)
        except OSError:
            log.exception("CLEANUP failed to remove temp file: %s", handle.file_path)

    # -- Recovery ---------------------------------------------------------

    def _attempt_recovery(self) -> None:
        log.info(
            "RECOVERY entered: attempt=%d/%d desired_pos=%.1f last_reported_pos=%.1f url=%s",
            self._recovery_attempts, self.MAX_RECOVERY_ATTEMPTS,
            self._desired_position, self.player.current_time, self.current_youtube_url,
        )
        if self._recovery_attempts >= self.MAX_RECOVERY_ATTEMPTS:
            log.error("RECOVERY giving up: max attempts (%d) reached", self.MAX_RECOVERY_ATTEMPTS)
            self.notify("Playback failed after multiple retries", severity="error")
            self._recovery_attempts = 0
            if isinstance(self.screen, PlayerScreen):
                self.pop_screen()
            return

        if not self.current_youtube_url:
            log.warning("RECOVERY aborted: no URL (index=%d)", self.current_index)
            return

        self._recovery_attempts += 1
        # Prefer the position the user was actually trying to reach (e.g. the
        # target of a seek that ran past the buffer) over the last-reported
        # current_time, which can lag behind a seek that failed immediately.
        last_position = max(self.player.current_time, self._desired_position)
        log.info("RECOVERY attempt %d/%d — re-extracting URL, will seek to %.1fs",
                 self._recovery_attempts, self.MAX_RECOVERY_ATTEMPTS, last_position)
        self._play_video_async(self.current_youtube_url, self.current_title, seek_to=last_position)

    # -- Player callbacks (from player thread) ----------------------------

    def _on_time_update(self, current_time: float, duration: float) -> None:
        try:
            self.call_from_thread(self._update_player_progress, current_time, duration)
        except Exception:
            log.exception("_on_time_update call_from_thread failed")

    def _update_player_progress(self, current_time: float, duration: float) -> None:
        self._desired_position = current_time
        ps = self.screen
        if isinstance(ps, PlayerScreen):
            ps.update_progress(current_time, duration)
        # Persist position every 5s for resume on crash/quit.
        now = time.monotonic()
        if now - self._last_pos_save > 5.0:
            self._save_resume_data()
            self._last_pos_save = now

    def _on_player_error(self, message: str) -> None:
        log.error("Player error: %s", message)
        try:
            self.call_from_thread(self.notify, message, title="Player Error", severity="error")
        except Exception:
            log.exception("_on_player_error call_from_thread failed")

    def _on_track_end(self, error_msg: Optional[str] = None) -> None:
        log.info("ON_TRACK_END fired on thread=%s, calling call_from_thread (blocks until UI thread services it)",
                  threading.current_thread().name)
        try:
            self.call_from_thread(self._handle_track_end, error_msg)
        except Exception:
            log.exception("_on_track_end call_from_thread failed")
        log.info("ON_TRACK_END call_from_thread returned on thread=%s", threading.current_thread().name)

    def _handle_track_end(self, error_msg: Optional[str] = None) -> None:
        log.info(
            "TRACK_END handled: error_msg=%s pos=%.1f desired_pos=%.1f recovery_attempts=%d",
            error_msg, self.player.current_time, self._desired_position, self._recovery_attempts,
        )
        # A "normal" end-file while the backing download hasn't finished yet
        # means mpv hit the current end of a still-growing file, not the real
        # end of the track (e.g. a resume/recovery seek landed ahead of what's
        # downloaded so far). Treat that as recoverable instead of advancing.
        download = self._active_download
        if not error_msg and download is not None and not download.is_done:
            error_msg = (
                f"Playback ended prematurely at {self.player.current_time:.1f}s "
                "(download still in progress)"
            )
            log.warning(
                "TRACK_END normal-end while download unfinished -> treating as recoverable (pos=%.1f)",
                self.player.current_time,
            )
        if error_msg:
            log.error("TRACK_END with error -> starting recovery: %s", error_msg)
            log.info("HANDLE_TRACK_END -> recovery")
            self._attempt_recovery()
        else:
            log.info("TRACK_END normal -> advancing")
            log.info("HANDLE_TRACK_END -> advance_to_next")
            self._recovery_attempts = 0
            self._advance_to_next()

    def _advance_to_next(self) -> None:
        log.info(
            "ADVANCE_TO_NEXT entered: current_index=%s results=%s current_screen=%s",
            self.current_index,
            len(self.results),
            type(self.screen).__name__,
        )
        next_index = self.current_index + 1
        log.info(
            "ADVANCE_TO_NEXT calculated next_index=%s",
            next_index,
        )
        if 0 <= next_index < len(self.results):
            log.info(
                "ADVANCE_TO_NEXT playing next track index=%s",
                next_index,
            )
            log.info("Advancing to next track [%d/%d]", next_index + 1, len(self.results))
            self.play_at(next_index)
        else:
            log.info("Queue exhausted — finished after %d tracks", len(self.results))
            log.info("ADVANCE_TO_NEXT popping PlayerScreen")
            self.current_index = -1
            self.current_title = ""
            self.current_youtube_url = ""
            self._cleanup_active_download()
            self.notify("Playback finished", timeout=2)
            if isinstance(self.screen, PlayerScreen):
                self.pop_screen()

    # -- Keyboard actions -------------------------------------------------

    def action_go_to_position(self) -> None:
        if isinstance(self.screen, SeekModal):
            return
        if not self.player.process:
            self.notify("Nothing playing", title="Seek", timeout=2)
            return
        duration = self.player.duration
        if duration <= 0:
            self.notify("Track duration unknown yet", title="Seek", timeout=2)
            return

        def _on_seek(pos: Optional[float]) -> None:
            if pos is not None:
                self._desired_position = pos
                log.info("USER ACTION: go to position %.1fs", pos)
                self.player.seek_absolute(pos)
                self.notify(f"Seeked to {PlayerScreen._fmt(pos)}", timeout=2)

        self.push_screen(SeekModal(duration), _on_seek)

    def action_toggle_pause(self) -> None:
        if self.player.process:
            self.player.toggle_pause()
            log.info("Toggle pause (is_playing=%s)", self.player.is_playing)

    def action_seek_forward(self) -> None:
        if self.player.process:
            self._desired_position = self.player.current_time + 5
            log.info("USER ACTION: seek forward +5s requested (from pos=%.1f, desired=%.1f)",
                      self.player.current_time, self._desired_position)
            self.player.seek(5)
        else:
            log.info("USER ACTION: seek forward ignored, no active player")

    def action_seek_backward(self) -> None:
        if self.player.process:
            self._desired_position = max(0.0, self.player.current_time - 5)
            log.info("USER ACTION: seek backward -5s requested (from pos=%.1f, desired=%.1f)",
                      self.player.current_time, self._desired_position)
            self.player.seek(-5)
        else:
            log.info("USER ACTION: seek backward ignored, no active player")

    def action_volume_up(self) -> None:
        if self.player.process:
            self.player.volume_up()
            log.info("Volume up -> %d", self.player.volume)
            self.notify(f"Volume {self.player.volume:.0f}", title="Volume", timeout=1)

    def action_volume_down(self) -> None:
        if self.player.process:
            self.player.volume_down()
            log.info("Volume down -> %d", self.player.volume)
            self.notify(f"Volume {self.player.volume:.0f}", title="Volume", timeout=1)

    def action_speed_up(self) -> None:
        if self.player.process:
            self.player.speed_up()
            log.info("Speed up -> %.2fx", self.player.speed)
            self.notify(f"Speed {self.player.speed:.2f}x", title="Speed", timeout=1)

    def action_speed_down(self) -> None:
        if self.player.process:
            self.player.speed_down()
            log.info("Speed down -> %.2fx", self.player.speed)
            self.notify(f"Speed {self.player.speed:.2f}x", title="Speed", timeout=1)

    def action_next_track(self) -> None:
        if not self.results:
            log.warning("Next: no results loaded")
            self.notify("No results loaded", title="Next", timeout=1)
            return
        if self.current_index < len(self.results) - 1:
            log.info("Next track from index %d", self.current_index)
            self.play_at(self.current_index + 1)
        else:
            log.info("Next: already at last track")
            self.notify("Already at last track", title="Next", timeout=1)

    def action_prev_track(self) -> None:
        if not self.results:
            log.warning("Prev: no results loaded")
            self.notify("No results loaded", title="Prev", timeout=1)
            return
        if self.current_index > 0:
            log.info("Prev track from index %d", self.current_index)
            self.play_at(self.current_index - 1)
        else:
            log.info("Prev: already at first track")
            self.notify("Already at first track", title="Prev", timeout=1)


if __name__ == "__main__":
    app = YouTubePlayerApp()
    app.run()