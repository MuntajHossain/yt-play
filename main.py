import logging
import os
from typing import Optional
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Header, Footer, Input, OptionList, Label, ProgressBar
from textual.widgets.option_list import Option
from textual import work

from search import search_youtube, start_audio_download, wait_for_file_growth, DownloadHandle
from player import MpvPlayer

os.makedirs("log", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join("log", "yt-play.log")),
    ],
)
log = logging.getLogger("yt-play")


# ---------------------------------------------------------------------------
# Modal
# ---------------------------------------------------------------------------

class QuitScreen(ModalScreen[bool]):
    """Keyboard-driven quit confirmation. Y / N / Escape."""
    BINDINGS = [
        ("y", "yes", "Yes"),
        ("n", "no", "No"),
        ("escape", "no", "Cancel"),
    ]
    def compose(self) -> ComposeResult:
        yield Label("Quit YouTube Player? (y/n)")
    def action_yes(self) -> None:
        self.dismiss(True)
    def action_no(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

class SearchScreen(Screen):
    CSS = """
    SearchScreen { align: center middle; }
    SearchScreen > Vertical { width: 60; height: auto; }
    Input { margin-bottom: 1; }
    Label { text-align: center; }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Input(placeholder="Search YouTube...", id="search_input")
            yield Label("Press Enter to search", id="search_status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search_input" and event.input.value:
            self.query_one("#search_status", Label).update("Searching...")
            app: YouTubePlayerApp = self.app  # type: ignore
            app.do_search(event.input.value)


class ResultsScreen(Screen):
    CSS = """
    ResultsScreen { layout: vertical; }
    #results_title { padding: 0 1; }
    #results_list { height: 1fr; }
    #results_help { padding: 0 1; text-style: dim; }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Search Results", id="results_title")
        yield OptionList(id="results_list")
        yield Label("[Esc] Back to search  —  [N]ext  [P]rev  available during playback", id="results_help")
        yield Footer()

    def on_mount(self) -> None:
        app: YouTubePlayerApp = self.app  # type: ignore
        results = app.results
        title = self.query_one("#results_title", Label)
        option_list = self.query_one("#results_list", OptionList)
        title.update(f"Search Results ({len(results)})")
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
            "[N]ext  [P]rev  [[/]] Speed ∓0.25  [Esc] Back  [Ctrl+D] Quit",
            id="controls_help",
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

        self._recovery_attempts: int = 0
        self.MAX_RECOVERY_ATTEMPTS: int = 3
        self._desired_position: float = 0.0

        self._active_download: Optional[DownloadHandle] = None

    # -- Navigation -------------------------------------------------------

    def on_mount(self) -> None:
        self.push_screen(SearchScreen())

    def action_quit(self) -> None:
        if isinstance(self.screen, (ResultsScreen, PlayerScreen)):
            self.player.stop()
            self._cleanup_active_download()
            while not isinstance(self.screen, SearchScreen):
                self.pop_screen()
            return

        def _cb(result: bool) -> None:
            if result:
                self.player.stop()
                self._cleanup_active_download()
                self.exit()
        self.push_screen(QuitScreen(), _cb)

    # -- Search -----------------------------------------------------------

    @work(exclusive=True)
    async def do_search(self, query: str) -> None:
        log.info("Searching: %s", query)
        self.results = await search_youtube(query)
        log.info("Search returned %d results", len(self.results))
        self.current_index = -1
        self.current_title = ""
        self.current_youtube_url = ""
        self._recovery_attempts = 0
        self.push_screen(ResultsScreen())

    # -- Playback ---------------------------------------------------------

    def play_at(self, index: int) -> None:
        if index < 0 or index >= len(self.results):
            log.warning("play_at: invalid index %d (results: %d)", index, len(self.results))
            return
        self.current_index = index
        result = self.results[index]
        self.current_title = result.title
        self.current_youtube_url = result.url
        log.info("Playing [%d/%d]: %s (%s)", index + 1, len(self.results), result.title, result.url)
        self._recovery_attempts = 0
        self._desired_position = 0.0
        self._play_video_async(result.url, result.title)
        self.push_screen(PlayerScreen())

    @work(exclusive=True)
    async def _play_video_async(self, url: str, title: str, seek_to: float = 0.0) -> None:
        log.info("PLAY_VIDEO_ASYNC start: url=%s title=%s seek_to=%.1f", url, title, seek_to)

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

        log.info("PLAY_VIDEO_ASYNC waiting for initial buffer at %s", handle.file_path)
        got_data = await wait_for_file_growth(handle.file_path, min_bytes=65536, timeout=15.0)

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
        self.player.play(handle.file_path)
        if seek_to > 0:
            log.info("PLAY_VIDEO_ASYNC issuing post-start absolute seek to %.1fs (recovery)", seek_to)
            self.player.seek_absolute(seek_to)
        else:
            log.info("PLAY_VIDEO_ASYNC no post-start seek needed (seek_to=%.1f)", seek_to)
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
        if not handle:
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

        if not self.current_youtube_url or self.current_index < 0:
            log.warning("RECOVERY aborted: no track info (url=%s, index=%d)", self.current_youtube_url, self.current_index)
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

    def _on_player_error(self, message: str) -> None:
        log.error("Player error: %s", message)
        try:
            self.call_from_thread(self.notify, message, title="Player Error", severity="error")
        except Exception:
            log.exception("_on_player_error call_from_thread failed")

    def _on_track_end(self, error_msg: Optional[str] = None) -> None:
        try:
            self.call_from_thread(self._handle_track_end, error_msg)
        except Exception:
            log.exception("_on_track_end call_from_thread failed")

    def _handle_track_end(self, error_msg: Optional[str] = None) -> None:
        log.info(
            "TRACK_END handled: error_msg=%s pos=%.1f desired_pos=%.1f recovery_attempts=%d",
            error_msg, self.player.current_time, self._desired_position, self._recovery_attempts,
        )
        if error_msg:
            log.error("TRACK_END with error -> starting recovery: %s", error_msg)
            self._attempt_recovery()
        else:
            log.info("TRACK_END normal -> advancing")
            self._recovery_attempts = 0
            self._advance_to_next()

    def _advance_to_next(self) -> None:
        next_index = self.current_index + 1
        if 0 <= next_index < len(self.results):
            log.info("Advancing to next track [%d/%d]", next_index + 1, len(self.results))
            self.play_at(next_index)
        else:
            log.info("Queue exhausted — finished after %d tracks", len(self.results))
            self.current_index = -1
            self.current_title = ""
            self.current_youtube_url = ""
            self._cleanup_active_download()
            self.notify("Playback finished", timeout=2)
            if isinstance(self.screen, PlayerScreen):
                self.pop_screen()

    # -- Keyboard actions -------------------------------------------------

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