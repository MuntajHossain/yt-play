from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Header, Footer, Input, OptionList, Label, ProgressBar
from textual.widgets.option_list import Option
from textual import work

from search import search_youtube, extract_audio_url
from player import MpvPlayer


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

    # -- Navigation -------------------------------------------------------

    def on_mount(self) -> None:
        self.push_screen(SearchScreen())

    def action_quit(self) -> None:
        def _cb(result: bool) -> None:
            if result:
                self.player.stop()
                if isinstance(self.screen, SearchScreen):
                    self.exit()
                else:
                    while not isinstance(self.screen, SearchScreen):
                        self.pop_screen()
        self.push_screen(QuitScreen(), _cb)

    # -- Search -----------------------------------------------------------

    @work(exclusive=True)
    async def do_search(self, query: str) -> None:
        self.results = await search_youtube(query)
        self.current_index = -1
        self.current_title = ""
        self.push_screen(ResultsScreen())

    # -- Playback ---------------------------------------------------------

    def play_at(self, index: int) -> None:
        if index < 0 or index >= len(self.results):
            return
        self.current_index = index
        result = self.results[index]
        self.current_title = result.title
        self._play_video_async(result.url, result.title)
        self.push_screen(PlayerScreen())

    @work(exclusive=True)
    async def _play_video_async(self, url: str, title: str) -> None:
        audio_url, err = await extract_audio_url(url)
        if audio_url:
            self.player.play(audio_url)
            player_screen = self.screen
            if isinstance(player_screen, PlayerScreen):
                player_screen.update_now_playing(title)
        else:
            self.notify(err or "Failed to load audio", title="Error", severity="error")
            if isinstance(self.screen, PlayerScreen):
                self.pop_screen()

    # -- Player callbacks (from player thread) ----------------------------

    def _on_time_update(self, current_time: float, duration: float) -> None:
        try:
            self.call_from_thread(self._update_player_progress, current_time, duration)
        except Exception:
            pass

    def _update_player_progress(self, current_time: float, duration: float) -> None:
        ps = self.screen
        if isinstance(ps, PlayerScreen):
            ps.update_progress(current_time, duration)

    def _on_player_error(self, message: str) -> None:
        try:
            self.call_from_thread(self.notify, message, title="Player Error", severity="error")
        except Exception:
            pass

    def _on_track_end(self) -> None:
        try:
            self.call_from_thread(self._advance_to_next)
        except Exception:
            pass

    def _advance_to_next(self) -> None:
        next_index = self.current_index + 1
        if 0 <= next_index < len(self.results):
            self.play_at(next_index)
        else:
            self.current_index = -1
            self.current_title = ""
            self.notify("Playback finished", timeout=2)
            # pop to the screen below PlayerScreen (ResultsScreen or SearchScreen)
            if isinstance(self.screen, PlayerScreen):
                self.pop_screen()

    # -- Keyboard actions -------------------------------------------------

    def action_toggle_pause(self) -> None:
        if self.player.process:
            self.player.toggle_pause()

    def action_seek_forward(self) -> None:
        if self.player.process:
            self.player.seek(5)

    def action_seek_backward(self) -> None:
        if self.player.process:
            self.player.seek(-5)

    def action_volume_up(self) -> None:
        if self.player.process:
            self.player.volume_up()
            self.notify(f"Volume {self.player.volume:.0f}", title="Volume", timeout=1)

    def action_volume_down(self) -> None:
        if self.player.process:
            self.player.volume_down()
            self.notify(f"Volume {self.player.volume:.0f}", title="Volume", timeout=1)

    def action_speed_up(self) -> None:
        if self.player.process:
            self.player.speed_up()
            self.notify(f"Speed {self.player.speed:.2f}x", title="Speed", timeout=1)

    def action_speed_down(self) -> None:
        if self.player.process:
            self.player.speed_down()
            self.notify(f"Speed {self.player.speed:.2f}x", title="Speed", timeout=1)

    def action_next_track(self) -> None:
        if not self.results:
            self.notify("No results loaded", title="Next", timeout=1)
            return
        if self.current_index < len(self.results) - 1:
            self.play_at(self.current_index + 1)
        else:
            self.notify("Already at last track", title="Next", timeout=1)

    def action_prev_track(self) -> None:
        if not self.results:
            self.notify("No results loaded", title="Prev", timeout=1)
            return
        if self.current_index > 0:
            self.play_at(self.current_index - 1)
        else:
            self.notify("Already at first track", title="Prev", timeout=1)


if __name__ == "__main__":
    app = YouTubePlayerApp()
    app.run()
