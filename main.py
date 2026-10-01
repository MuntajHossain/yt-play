import argparse
import asyncio
import glob
import json
import logging
import os
import random
import threading
import time
from typing import Optional
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Header, Footer, Input, OptionList, Label, ProgressBar, LoadingIndicator
from textual.widgets.option_list import Option
from textual.binding import Binding
from textual import work
from textual.worker import get_current_worker

from search import (
    search_youtube,
    start_audio_download,
    wait_for_file_growth,
    DownloadHandle,
    SearchResult,
    format_duration,
    _extract_video_id,
    _marker_path,
)
from config import CONFIG
import playlist

APP_VERSION = "0.2.0"


def _parse_cli_args() -> None:
    """Handle -h/--version before any of the module's heavy import-time side
    effects (log file creation, mpv-lib fetch) run. Guarded by __name__ so
    importing this module (tests, the entry point) never parses argv.
    """
    parser = argparse.ArgumentParser(
        prog="ytplay",
        description="Terminal-based YouTube audio player - search YouTube, "
        "stream audio via yt-dlp + mpv, resume where you left off.",
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    args = parser.parse_args()
    if args.version:
        print(f"ytplay {APP_VERSION}")
        raise SystemExit(0)


if __name__ == "__main__":
    _parse_cli_args()

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
        logging.FileHandler(CURRENT_LOG_FILE, encoding="utf-8"),
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


def _resolve_recent_ref(value: str, recent: list) -> Optional[str]:
    """Map a ``$N`` token to ``recent[N]`` (``$0`` = newest). ``None`` if *value*
    isn't a ``$N`` token or N is out of range."""
    if len(value) < 2 or value[0] != "$" or not value[1:].isdigit():
        return None
    index = int(value[1:])
    return recent[index] if index < len(recent) else None


def _play_notification_sound() -> None:
    """Short audible cue that search results are ready. Never raises."""
    try:
        import winsound
    except ImportError:
        print("\a", end="", flush=True)
        return

    def _chime() -> None:
        # Rising three-note arpeggio (C5-E5-G5): distinct from Windows' own alert sounds.
        # winsound.Beep blocks, so this runs on a daemon thread.
        try:
            for freq, ms in ((523, 90), (659, 90), (784, 160)):
                winsound.Beep(freq, ms)
        except Exception:
            log.exception("Notification sound failed")

    threading.Thread(target=_chime, name="notify-chime", daemon=True).start()

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
    #menu_list { margin-top: 1; height: 6; }
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
        menu.add_option(Option("📃  Playlists", id="menu_playlists"))
        menu.focus()

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        app: YouTubePlayerApp = self.app  # type: ignore
        if event.option_id == "menu_history":
            app.go_to_history()
        elif event.option_id == "menu_search":
            app.go_to_search()
        elif event.option_id == "menu_url":
            app.action_play_from_url()
        elif event.option_id == "menu_playlists":
            app.go_to_playlists()

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

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Snapshot of the entries currently on screen, newest-first (same
        # order as the OptionList) — frozen at populate time so a selection
        # always maps to what the user actually sees, even if playback in
        # the background upserts (and reorders) history while this screen
        # is open. Re-taken on every mount/resume, not on every play.
        self._entries: list = []

    def on_mount(self) -> None:
        self._populate_list()

    def on_screen_resume(self) -> None:
        # Refresh so returning here (e.g. Esc from PlayerScreen) shows the
        # up-to-date order — but note this is the ONLY time the order is
        # re-taken; it then stays frozen for the rest of this visit.
        self._populate_list()

    def _populate_list(self, highlight_index: Optional[int] = None) -> None:
        app: YouTubePlayerApp = self.app  # type: ignore
        history = app._read_history()
        option_list = self.query_one("#history_list", OptionList)
        empty_label = self.query_one("#history_empty", Label)
        option_list.clear_options()
        # Newest first — snapshot this order now; it's what on_option_list_
        # option_selected and _delete_selected will resolve indices against,
        # regardless of any later background reordering.
        self._entries = list(reversed(history))
        if not self._entries:
            option_list.display = False
            empty_label.display = True
            return
        empty_label.display = False
        option_list.display = True
        for i, entry in enumerate(self._entries):
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
        if event.option_index >= len(self._entries):
            return
        app: YouTubePlayerApp = self.app  # type: ignore
        app.play_history_entry(self._entries[event.option_index])

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.pop_screen()
        elif event.key in ("d", "delete"):
            self._delete_selected()

    def _delete_selected(self) -> None:
        option_list = self.query_one("#history_list", OptionList)
        index = option_list.highlighted
        if index is None or index >= len(self._entries):
            return
        app: YouTubePlayerApp = self.app  # type: ignore
        entry = self._entries[index]
        title = entry.get("title", "Unknown")
        video_id = entry.get("video_id")

        def _cb(confirmed: bool) -> None:
            if not confirmed:
                return
            deleted_title = app.delete_history_entry(video_id)
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
    #search_spinner { display: none; height: 1; margin-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield SearchInput(placeholder="Search YouTube... ($0, $1... = repeat recent)", id="search_input")
            yield LoadingIndicator(id="search_spinner")
            yield Label("Press Enter to search", id="search_status")
            yield Label("", id="recent_searches")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Input).focus()
        self._refresh_recent()

    def on_screen_resume(self) -> None:
        self.query_one("#search_status", Label).update("Press Enter to search")
        self._set_loading(False)
        self.query_one(Input).focus()
        self._refresh_recent()

    def _set_loading(self, visible: bool) -> None:
        self.query_one("#search_spinner", LoadingIndicator).styles.display = "block" if visible else "none"

    def _refresh_recent(self) -> None:
        app: YouTubePlayerApp = self.app  # type: ignore
        if app.recent_searches:
            lines = ["[bold]Recent:[/]"] + [f"  ${i}  {q}" for i, q in enumerate(app.recent_searches)]
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
        if value.startswith("$") and value[1:].isdigit():
            query = _resolve_recent_ref(value, app.recent_searches)
            if query is None:
                self.query_one("#search_status", Label).update(f"No recent search at {value}")
                return
            self.query_one("#search_status", Label).update(f"Repeating: {query}")
            self._set_loading(True)
            app.do_search(query, auto_play_first=True)
            return
        self.query_one("#search_status", Label).update("Searching...")
        self._set_loading(True)
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
    #results_spinner { display: none; height: 1; padding: 0 1; }
    #results_help { padding: 0 1; text-style: dim; }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Search Results", id="results_title")
        yield OptionList(id="results_list")
        yield Label("", id="results_status")
        yield LoadingIndicator(id="results_spinner")
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
        self.query_one("#results_spinner", LoadingIndicator).styles.display = "none"
        title.update(f"Search Results ({len(results)}) — Page {app.search_page}")
        option_list.clear_options()
        for i, res in enumerate(results):
            text = f"{res.title} [{res.duration_str}] - {res.uploader}"
            option_list.add_option(Option(text, id=f"result_{i}"))
        self.query_one("#results_list").focus()

    def _set_loading(self, visible: bool) -> None:
        self.query_one("#results_spinner", LoadingIndicator).styles.display = "block" if visible else "none"

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "results_list":
            app: YouTubePlayerApp = self.app  # type: ignore
            app.play_at(event.option_index)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.pop_screen()
        elif event.key == "pagedown":
            self.query_one("#results_status", Label).update("Loading...")
            self._set_loading(True)
            self.app.next_search_page()  # type: ignore
        elif event.key == "pageup":
            self.query_one("#results_status", Label).update("Loading...")
            self._set_loading(True)
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


class NewPlaylistModal(ModalScreen[str]):
    """Input modal for a new playlist name. Dismisses with the name or None."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    CSS = """
    NewPlaylistModal { align: center middle; }
    #new_playlist_dialog { width: 50; padding: 1 2; border: thick $primary; background: $surface; }
    #new_playlist_label { text-align: center; padding-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="new_playlist_dialog"):
            yield Label("New playlist name:", id="new_playlist_label")
            yield Input(placeholder="e.g. Chill Mix", id="new_playlist_input")

    def on_mount(self) -> None:
        self.query_one("#new_playlist_input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        name = event.input.value.strip()
        self.dismiss(name or None)


class AddToPlaylistModal(ModalScreen[str]):
    """Pick an existing playlist or type a new name. Dismisses with the
    chosen/typed playlist name, or None on cancel."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    CSS = """
    AddToPlaylistModal { align: center middle; }
    #add_to_playlist_dialog { width: 50; height: auto; padding: 1 2; border: thick $primary; background: $surface; }
    #add_to_playlist_label { text-align: center; padding-bottom: 1; }
    #existing_playlists { height: 8; margin-bottom: 1; }
    """

    def __init__(self, names: list) -> None:
        super().__init__()
        self._names = names

    def compose(self) -> ComposeResult:
        with Vertical(id="add_to_playlist_dialog"):
            yield Label("Add to Playlist", id="add_to_playlist_label")
            if self._names:
                yield OptionList(
                    *[Option(name, id=f"pl_{i}") for i, name in enumerate(self._names)],
                    id="existing_playlists",
                )
            yield Input(placeholder="Or type a new playlist name...", id="new_playlist_name_input")

    def on_mount(self) -> None:
        if self._names:
            self.query_one("#existing_playlists", OptionList).focus()
        else:
            self.query_one("#new_playlist_name_input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._names[event.option_index])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        name = event.input.value.strip()
        if name:
            self.dismiss(name)


class PlaylistsScreen(Screen):
    """List playlists; select to open, N to create, D to delete."""

    CSS = """
    PlaylistsScreen { layout: vertical; }
    #playlists_title { padding: 0 1; }
    #playlists_list { height: 1fr; }
    #playlists_empty { padding: 1; text-align: center; text-style: dim; }
    #playlists_help { padding: 0 1; text-style: dim; }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Playlists", id="playlists_title")
        yield OptionList(id="playlists_list")
        yield Label("No playlists yet", id="playlists_empty")
        yield Label("[Esc] Back  [Enter] Open  [N]ew  [D]elete", id="playlists_help", markup=False)
        yield Footer()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._playlists: list = []

    def on_mount(self) -> None:
        self._populate_list()

    def on_screen_resume(self) -> None:
        self._populate_list()

    def _populate_list(self) -> None:
        self._playlists = playlist.read_playlists()
        option_list = self.query_one("#playlists_list", OptionList)
        empty_label = self.query_one("#playlists_empty", Label)
        option_list.clear_options()
        if not self._playlists:
            option_list.display = False
            empty_label.display = True
            return
        empty_label.display = False
        option_list.display = True
        for i, pl in enumerate(self._playlists):
            option_list.add_option(Option(f"{pl.name}  ({len(pl.videos)} videos)", id=f"playlist_{i}"))
        option_list.focus()

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_index >= len(self._playlists):
            return
        self.app.push_screen(PlaylistDetailScreen(self._playlists[event.option_index].name))

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.pop_screen()
        elif event.key == "n":
            self._new_playlist()
        elif event.key in ("d", "delete"):
            self._delete_selected()

    def _new_playlist(self) -> None:
        def _cb(name: Optional[str]) -> None:
            if not name:
                return
            playlists = playlist.read_playlists()
            playlist.create_playlist(playlists, name)
            playlist.write_playlists(playlists)
            self._populate_list()

        self.app.push_screen(NewPlaylistModal(), _cb)

    def _delete_selected(self) -> None:
        option_list = self.query_one("#playlists_list", OptionList)
        index = option_list.highlighted
        if index is None or index >= len(self._playlists):
            return
        name = self._playlists[index].name

        def _cb(confirmed: bool) -> None:
            if not confirmed:
                return
            playlists = playlist.read_playlists()
            if playlist.delete_playlist(playlists, name):
                playlist.write_playlists(playlists)
                self.app.notify(f"Deleted playlist: {name}", timeout=2)
            self._populate_list()

        self.app.push_screen(QuitScreen(f"Delete playlist '{name}'? (y/n)"), _cb)


class PlaylistDetailScreen(Screen):
    """List videos in one playlist; select to play from there, D to remove."""

    CSS = """
    PlaylistDetailScreen { layout: vertical; }
    #playlist_detail_title { padding: 0 1; }
    #playlist_videos { height: 1fr; }
    #playlist_detail_empty { padding: 1; text-align: center; text-style: dim; }
    #playlist_detail_help { padding: 0 1; text-style: dim; }
    """

    def __init__(self, playlist_name: str, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._playlist_name = playlist_name
        self._videos: list = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label(self._playlist_name, id="playlist_detail_title")
        yield OptionList(id="playlist_videos")
        yield Label("No videos in this playlist yet", id="playlist_detail_empty")
        yield Label(
            "[Esc] Back  [Enter] Play from here  [D]elete video  —  "
            "[R]epeat and [S]huffle are global, available during playback",
            id="playlist_detail_help",
            markup=False,
        )
        yield Footer()

    def on_mount(self) -> None:
        self._populate_list()

    def on_screen_resume(self) -> None:
        self._populate_list()

    def _populate_list(self, highlight_index: Optional[int] = None) -> None:
        playlists = playlist.read_playlists()
        pl = playlist.get_playlist(playlists, self._playlist_name)
        self._videos = pl.videos if pl else []
        option_list = self.query_one("#playlist_videos", OptionList)
        empty_label = self.query_one("#playlist_detail_empty", Label)
        title = self.query_one("#playlist_detail_title", Label)
        title.update(f"{self._playlist_name}  ({len(self._videos)} videos)")
        option_list.clear_options()
        if not self._videos:
            option_list.display = False
            empty_label.display = True
            return
        empty_label.display = False
        option_list.display = True
        for i, v in enumerate(self._videos):
            option_list.add_option(Option(f"{v.title}  [{v.duration_str}] - {v.uploader}", id=f"plvid_{i}"))
        option_list.focus()
        if highlight_index is not None and option_list.option_count:
            option_list.highlighted = min(highlight_index, option_list.option_count - 1)

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_index >= len(self._videos):
            return
        app: YouTubePlayerApp = self.app  # type: ignore
        app.play_playlist(self._playlist_name, start_index=event.option_index)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.pop_screen()
        elif event.key in ("d", "delete"):
            self._delete_selected()

    def _delete_selected(self) -> None:
        option_list = self.query_one("#playlist_videos", OptionList)
        index = option_list.highlighted
        if index is None or index >= len(self._videos):
            return
        video = self._videos[index]

        def _cb(confirmed: bool) -> None:
            if not confirmed:
                return
            playlists = playlist.read_playlists()
            if playlist.remove_video_from_playlist(playlists, self._playlist_name, video.id):
                playlist.write_playlists(playlists)
                self.app.notify(f"Removed: {video.title}", timeout=2)
            self._populate_list(highlight_index=index)

        self.app.push_screen(QuitScreen(f"Remove '{video.title}' from playlist? (y/n)"), _cb)


class PlayerScreen(Screen):
    CSS = """
    PlayerScreen { layout: vertical; }
    #now_playing { padding: 1; text-align: center; }
    #progress_container { height: auto; align: center middle; margin: 0 2; }
    #time_current, #time_total { width: 8; text-align: center; }
    ProgressBar { width: 1fr; margin: 0 1; }
    #download_status { padding: 0 1; text-align: center; text-style: italic; color: $warning; }
    #download_spinner { display: none; height: 1; }
    #playback_modes { padding: 0 1; text-align: center; text-style: bold; color: $success; height: 1; }
    #controls_help { padding: 0 1; text-align: center; text-style: dim; }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Now Playing: Nothing", id="now_playing")
        with Horizontal(id="progress_container"):
            yield Label("00:00", id="time_current")
            yield ProgressBar(total=100, show_eta=False, id="progress_bar")
            yield Label("00:00", id="time_total")
        yield LoadingIndicator(id="download_spinner")
        yield Label("", id="download_status")
        yield Label("", id="playback_modes")
        yield Label(
            "[Space] Play/Pause  [Left/Right] Seek ±5s  [Up/Down] Vol ±5  "
            "[G]o to position  [N]ext  [P]rev  [/] Speed ∓0.25  [Ctrl+P] Add to Playlist  "
            "[R]epeat  [S]huffle  [Esc] Back  [Ctrl+D] Quit",
            id="controls_help",
            markup=False,
        )
        yield Footer()

    def on_mount(self) -> None:
        app: YouTubePlayerApp = self.app  # type: ignore
        self._update_now_playing(app.current_title)
        self.update_modes(app.repeat_enabled, app.shuffle_enabled)

    def update_now_playing(self, title: str) -> None:
        self._update_now_playing(title)

    def _update_now_playing(self, title: str) -> None:
        label = self.query_one("#now_playing", Label)
        if title:
            label.update(f"Now Playing: {title}")
        else:
            label.update("Now Playing: Nothing")

    def update_modes(self, repeat_enabled: bool, shuffle_enabled: bool) -> None:
        parts = []
        if repeat_enabled:
            parts.append("Repeat: On")
        if shuffle_enabled:
            parts.append("Shuffle: On")
        self.query_one("#playback_modes", Label).update("  ".join(parts))

    def set_downloading(self, visible: bool) -> None:
        self.query_one("#download_spinner", LoadingIndicator).styles.display = "block" if visible else "none"
        self.query_one("#download_status", Label).update("Downloading..." if visible else "")

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
        ("ctrl+p", "add_to_playlist", "Add to Playlist"),
        ("r", "toggle_repeat", "Repeat"),
        ("s", "toggle_shuffle", "Shuffle"),
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
        # True while self.results holds a playlist's videos rather than search
        # results — suppresses history-position resume (a playlist play is an
        # explicit "from the top" action) and is reset whenever another source
        # (search/history/URL) repopulates self.results.
        self._queue_is_playlist: bool = False

        self.repeat_enabled: bool = False  # replays the current track on natural end
        self.shuffle_enabled: bool = False
        self._shuffle_order: list = []  # permutation of range(len(self.results)), regenerated lazily

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
        self._active_download_worker = None

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

    def go_to_playlists(self) -> None:
        self.push_screen(PlaylistsScreen())

    def play_history_entry(self, entry: dict) -> None:
        """Play a specific history entry (as shown by HistoryScreen). Takes
        the entry itself rather than a position/index — background playback
        upserts history (moving the just-played video to the top) while
        HistoryScreen may be sitting open with an older snapshot on screen,
        so resolving by index against a fresh read would silently play a
        different entry than the one the user actually selected."""
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
                self._queue_is_playlist = False
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
            self._queue_is_playlist = False
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
        self._queue_is_playlist = False
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

    def delete_history_entry(self, video_id: Optional[str]) -> Optional[str]:
        """Delete the history entry matching *video_id*. Matches by identity
        rather than position — same reasoning as play_history_entry — so a
        HistoryScreen showing a stale (pre-reorder) snapshot still deletes
        the entry the user actually picked. Returns the deleted entry's
        title, or None if not found."""
        if not video_id:
            return None
        history = self._read_history()
        for i, entry in enumerate(history):
            if entry.get("video_id") == video_id:
                history.pop(i)
                self._write_history(history)
                log.info("HISTORY deleted: %s (%d entries remain)", video_id, len(history))
                return entry.get("title", "Unknown")
        return None

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
                    if self._active_download_worker is not None:
                        self._active_download_worker.cancel()
                        self._active_download_worker = None
                    self._cleanup_active_download()
                    log.info("AFTER cleanup")

                    while isinstance(self.screen, PlayerScreen):
                        log.info("POPPING PlayerScreen")
                        self.pop_screen()

                    log.info("DONE")
            self.push_screen(QuitScreen("Stop playback and return to results? (y/n)"), _cb)
            return

        self.player.stop()
        if self._active_download_worker is not None:
            self._active_download_worker.cancel()
            self._active_download_worker = None
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
        _play_notification_sound()
        self.current_index = -1
        self.current_title = ""
        self.current_youtube_url = ""
        self._queue_is_playlist = False
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
        _play_notification_sound()
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
            self.screen._set_loading(False)

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
        self._play_video_async(result.url, result.title, resume=not self._queue_is_playlist)
        log.info("PLAY_AT pushing PlayerScreen")
        self.push_screen(PlayerScreen())

    @staticmethod
    def _buffer_wait_params(seek_to: float) -> tuple:
        """min_bytes / timeout / stall_timeout for wait_for_file_growth().

        min_bytes must scale with seek_to (need enough of the file on disk to
        cover the resume timestamp) — that's a correctness requirement, not a
        speed guess. timeout is just the startup grace period, kept flat: no
        need to scale it, since stall_timeout is what actually bounds the
        wait. stall_timeout scales instead, because a real download can go
        quiet for tens of seconds (throttling, a slow patch of network) and
        still be perfectly healthy — a hard deadline scaled to an optimistic
        ~20x-realtime download speed killed downloads that were merely a bit
        slower than that but still making steady progress (logged as
        "PLAY_VIDEO_ASYNC timed out waiting for download to produce data"
        while the same download quietly finished a few minutes later in the
        background). Scaling patience instead of the deadline tolerates that
        without waiting forever on a genuinely dead download.
        """
        min_bytes = 65536
        buffer_timeout = 15.0
        stall_timeout = 20.0
        if seek_to > 0:
            min_bytes = max(min_bytes, int(seek_to * 20000))  # ~160kbps conservative estimate
            stall_timeout = max(stall_timeout, seek_to * 0.02)  # tolerate longer quiet patches on deep resumes
        return min_bytes, buffer_timeout, stall_timeout

    @work
    async def _play_video_async(self, url: str, title: str, seek_to: float = 0.0, resume: bool = True) -> None:
        log.info("PLAY_VIDEO_ASYNC start: url=%s title=%s seek_to=%.1f resume=%s", url, title, seek_to, resume)

        # Resume from saved position if this video has a history entry.
        if seek_to == 0.0 and resume:
            vid = _extract_video_id(url)
            if vid:
                saved = self._lookup_history_position(vid)
                if saved and saved > 0:
                    seek_to = saved
                    self.notify(f"Resumed at {PlayerScreen._fmt(seek_to)}", timeout=3)
                    log.info("RESUME applied: %s at %.1fs", vid, seek_to)

        # Cancel any previous _play_video_async worker still running (e.g.
        # one still stuck in the buffer-wait for a track we've since
        # switched away from, or quit out of) so it can't outlive us and
        # fire a stale "timed out starting download" notification long
        # after the user has already moved on — it was awaiting a file that
        # _cleanup_active_download() below is about to delete out from
        # under it anyway.
        prev_worker = self._active_download_worker
        self._active_download_worker = get_current_worker()
        if prev_worker is not None and prev_worker is not self._active_download_worker:
            prev_worker.cancel()

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

        if not handle.is_cached and isinstance(self.screen, PlayerScreen):
            self.screen.set_downloading(True)

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
        min_bytes, buffer_timeout, stall_timeout = self._buffer_wait_params(seek_to)

        if handle.is_cached:
            # Cached file is already complete — no need to wait for it to grow.
            # The min_bytes estimate can exceed the file's actual size (it's a
            # conservative bitrate guess), which would otherwise make a complete
            # cached file look like it never buffered enough and stall forever.
            got_data = os.path.exists(handle.file_path)
        else:
            log.info(
                "PLAY_VIDEO_ASYNC waiting for initial buffer (min_bytes=%d timeout=%.1f stall_timeout=%.1f) at %s",
                min_bytes, buffer_timeout, stall_timeout, handle.file_path,
            )
            got_data = await wait_for_file_growth(
                handle.file_path, min_bytes=min_bytes, timeout=buffer_timeout, stall_timeout=stall_timeout
            )

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
            player_screen.set_downloading(False)

        # Let the download finish in the background; log its outcome.
        await handle.wait()
        if handle.error and handle is self._active_download:
            log.error("PLAY_VIDEO_ASYNC background download ended with error: %s", handle.error)

    def _cleanup_active_download(self) -> None:
        """Kill any in-progress download and remove its temp file.

        A download that already finished successfully has its .done marker
        written (see DownloadHandle.wait()) and is a valid cache entry — must
        not delete that file, or every video ever played to completion (or
        one whose background download simply raced ahead of playback and
        finished) gets its cache silently destroyed on quit, forcing a full
        re-download next time despite having a marker on disk.

        Checked via the marker file itself, not handle.is_done/handle.error:
        asyncio updates a subprocess's returncode (so is_done can go True)
        the moment the OS process exits, independently of whether anyone
        has awaited handle.wait() — and the marker is only written inside
        wait(). If we bailed out of the buffer-wait early (timed out) without
        ever reaching `await handle.wait()`, is_done/error reflect nothing
        about how the download actually went; the marker is the only
        trustworthy signal that it finished cleanly.
        """
        handle = self._active_download
        self._active_download = None
        if not handle or handle.is_cached:
            return
        handle.kill()
        if handle.video_id and os.path.exists(_marker_path(handle.video_id)):
            log.info("CLEANUP keeping completed download (has .done marker): %s", handle.file_path)
            return
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
            "ADVANCE_TO_NEXT entered: current_index=%s results=%s current_screen=%s repeat=%s",
            self.current_index,
            len(self.results),
            type(self.screen).__name__,
            self.repeat_enabled,
        )
        if self.repeat_enabled:
            log.info("ADVANCE_TO_NEXT repeat-one: replaying index=%s", self.current_index)
            self.play_at(self.current_index)
            return
        next_index = self._resolve_step_index(1)
        log.info(
            "ADVANCE_TO_NEXT calculated next_index=%s",
            next_index,
        )
        if next_index is not None:
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
            if self._active_download_worker is not None:
                self._active_download_worker.cancel()
                self._active_download_worker = None
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
        next_index = self._resolve_step_index(1)
        if next_index is not None:
            log.info("Next track from index %d", self.current_index)
            self.play_at(next_index)
        else:
            log.info("Next: already at last track")
            self.notify("Already at last track", title="Next", timeout=1)

    def action_prev_track(self) -> None:
        if not self.results:
            log.warning("Prev: no results loaded")
            self.notify("No results loaded", title="Prev", timeout=1)
            return
        prev_index = self._resolve_step_index(-1)
        if prev_index is not None:
            log.info("Prev track from index %d", self.current_index)
            self.play_at(prev_index)
        else:
            log.info("Prev: already at first track")
            self.notify("Already at first track", title="Prev", timeout=1)

    # -- Repeat / Shuffle ---------------------------------------------------

    def _resolve_step_index(self, direction: int) -> Optional[int]:
        """Index for the next/prev track (direction=+1/-1), respecting
        shuffle order. Never wraps the queue — repeat is single-track only
        and doesn't affect manual/auto-advance navigation past the ends."""
        n = len(self.results)
        if n == 0:
            return None
        if self.shuffle_enabled:
            if len(self._shuffle_order) != n:
                self._regenerate_shuffle_order()
            pos = self._shuffle_order.index(self.current_index) if self.current_index in self._shuffle_order else 0
            pos += direction
            if 0 <= pos < n:
                return self._shuffle_order[pos]
            return None
        idx = self.current_index + direction
        if 0 <= idx < n:
            return idx
        return None

    def _regenerate_shuffle_order(self) -> None:
        indices = list(range(len(self.results)))
        random.shuffle(indices)
        self._shuffle_order = indices

    def action_toggle_repeat(self) -> None:
        self.repeat_enabled = not self.repeat_enabled
        self.notify(f"Repeat: {'on' if self.repeat_enabled else 'off'}", title="Repeat", timeout=1)
        if isinstance(self.screen, PlayerScreen):
            self.screen.update_modes(self.repeat_enabled, self.shuffle_enabled)

    def action_toggle_shuffle(self) -> None:
        self.shuffle_enabled = not self.shuffle_enabled
        if self.shuffle_enabled:
            self._regenerate_shuffle_order()
        self.notify(f"Shuffle: {'on' if self.shuffle_enabled else 'off'}", title="Shuffle", timeout=1)
        if isinstance(self.screen, PlayerScreen):
            self.screen.update_modes(self.repeat_enabled, self.shuffle_enabled)

    # -- Playlists ----------------------------------------------------------

    def play_playlist(self, playlist_name: str, start_index: int = 0) -> None:
        playlists = playlist.read_playlists()
        pl = playlist.get_playlist(playlists, playlist_name)
        if pl is None or not pl.videos:
            self.notify("Playlist is empty", title="Playlist", severity="warning")
            return
        self.results = [
            SearchResult(id=v.id, title=v.title, url=v.url, duration_str=v.duration_str, uploader=v.uploader)
            for v in pl.videos
        ]
        self._shuffle_order = []
        self._queue_is_playlist = True
        self.play_at(start_index)

    def action_add_to_playlist(self) -> None:
        video = self._resolve_current_video_for_playlist()
        if video is None:
            self.notify("No video selected", title="Playlist", severity="warning")
            return
        names = playlist.list_playlist_names(playlist.read_playlists())
        self.push_screen(AddToPlaylistModal(names), lambda name: self._on_playlist_chosen(name, video))

    def _resolve_current_video_for_playlist(self) -> Optional[dict]:
        """Video info to add via Ctrl+P, based on the currently visible
        screen — PlayerScreen's now-playing video, or the highlighted item
        in ResultsScreen/HistoryScreen. Returns a dict shaped like
        PlaylistEntry (minus added_at), or None if nothing is selected."""
        screen = self.screen
        if isinstance(screen, PlayerScreen):
            if not self.current_youtube_url:
                return None
            vid = _extract_video_id(self.current_youtube_url)
            if 0 <= self.current_index < len(self.results) and self.results[self.current_index].url == self.current_youtube_url:
                r = self.results[self.current_index]
                return {"id": vid or r.id, "title": r.title, "url": r.url, "duration_str": r.duration_str, "uploader": r.uploader}
            return {"id": vid or "", "title": self.current_title, "url": self.current_youtube_url, "duration_str": "", "uploader": ""}
        if isinstance(screen, ResultsScreen):
            index = screen.query_one("#results_list", OptionList).highlighted
            if index is None or index >= len(self.results):
                return None
            r = self.results[index]
            return {"id": r.id, "title": r.title, "url": r.url, "duration_str": r.duration_str, "uploader": r.uploader}
        if isinstance(screen, HistoryScreen):
            index = screen.query_one("#history_list", OptionList).highlighted
            if index is None or index >= len(screen._entries):
                return None
            entry = screen._entries[index]
            return {
                "id": entry.get("video_id", ""),
                "title": entry.get("title", "Unknown"),
                "url": entry.get("url", ""),
                "duration_str": format_duration(entry.get("duration", 0)),
                "uploader": "",
            }
        return None

    def _on_playlist_chosen(self, name: Optional[str], video: dict) -> None:
        if not name or not name.strip():
            return
        name = name.strip()
        playlists = playlist.read_playlists()
        existing = playlist.get_playlist(playlists, name)
        created = existing is None
        playlist.create_playlist(playlists, name)
        entry = playlist.PlaylistEntry(
            id=video["id"],
            title=video["title"],
            url=video["url"],
            duration_str=video["duration_str"],
            uploader=video["uploader"],
            added_at=time.time(),
        )
        added = playlist.add_video_to_playlist(playlists, name, entry)
        playlist.write_playlists(playlists)
        if added:
            suffix = " (new playlist)" if created else ""
            self.notify(f"Added to '{name}'{suffix}", title="Playlist", timeout=2)
        else:
            self.notify(f"Already in '{name}'", title="Playlist", timeout=2)


if __name__ == "__main__":
    app = YouTubePlayerApp()
    app.run()