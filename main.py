from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Header, Footer, Input, OptionList
from textual import work

from search import search_youtube, extract_audio_url
from player import MpvPlayer
from ui_components import SearchWidget, ResultsWidget, PlayerControlWidget


class YouTubePlayerApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }

    SearchWidget {
        height: auto;
        padding: 1;
        border: solid green;
    }

    ResultsWidget {
        height: 1fr;
        padding: 1;
        border: solid blue;
    }

    PlayerControlWidget {
        height: auto;
        padding: 1;
        border: solid red;
    }

    #progress_container {
        height: auto;
        align: center middle;
    }

    ProgressBar {
        width: 1fr;
        margin: 0 2;
    }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("space", "toggle_pause", "Play/Pause"),
        ("right", "seek_forward", "Seek +5s"),
        ("left", "seek_backward", "Seek -5s"),
        ("up", "volume_up", "Vol +5"),
        ("down", "volume_down", "Vol -5"),
        ("n", "next_track", "Next"),
        ("p", "prev_track", "Prev"),
    ]

    def __init__(self):
        super().__init__()
        self.player = MpvPlayer()
        self.player.on_time_update = self._on_time_update
        self.player.on_error = self._on_player_error
        self.player.on_end = self._on_track_end

        # Cached widget references (set in on_mount)
        self._search_widget: SearchWidget = None  # type: ignore
        self._results_widget: ResultsWidget = None  # type: ignore
        self._player_ctrl: PlayerControlWidget = None  # type: ignore

        # Playback state
        self._results: list = []
        self._current_index: int = -1
        self._current_title: str = ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container():
            yield SearchWidget()
            yield ResultsWidget()
            yield PlayerControlWidget()
        yield Footer()

    def on_mount(self) -> None:
        self._search_widget = self.query_one(SearchWidget)
        self._results_widget = self.query_one(ResultsWidget)
        self._player_ctrl = self.query_one(PlayerControlWidget)

    def action_quit(self) -> None:
        self.player.stop()
        self.exit()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search_input":
            query = event.input.value
            if query:
                self.action_search(query)

    @work(exclusive=True)
    async def action_search(self, query: str) -> None:
        self._results_widget.show_loading(f"Searching for '{query}'...")
        results = await search_youtube(query)
        self._results = results
        self._results_widget.populate(results)
        count = len(results)
        self._results_widget.show_count(count)

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    def _play_at(self, index: int):
        """Play the result at *index* (adds all results as a queue)."""
        if index < 0 or index >= len(self._results):
            return

        self._current_index = index
        result = self._results[index]
        self._current_title = result.title
        self._player_ctrl.update_status(False, f"Loading {result.title}...")
        self._play_video_async(result.url, result.title)

    @work(exclusive=True)
    async def _play_video_async(self, url: str, title: str) -> None:
        audio_url = await extract_audio_url(url)
        if audio_url:
            self.player.play(audio_url)
            self._player_ctrl.update_status(True, title)
        else:
            self._player_ctrl.update_status(False, "Failed to load audio")
            self.notify("Failed to load audio", title="Error", severity="error")

    async def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        if event.option_list.id == "results_list":
            self._play_at(event.option_index)

    # ------------------------------------------------------------------
    # Player callbacks (called from player thread)
    # ------------------------------------------------------------------

    def _on_time_update(self, current_time: float, duration: float) -> None:
        try:
            self.call_from_thread(
                self._player_ctrl.update_progress, current_time, duration
            )
        except Exception:
            pass

    def _on_player_error(self, message: str) -> None:
        self.call_from_thread(
            self.notify, message, title="Player Error", severity="error"
        )

    def _on_track_end(self) -> None:
        """Called when mpv finishes playing a track."""
        self.call_from_thread(self._advance_to_next)

    def _advance_to_next(self):
        """Advance to the next track in the queue, if available."""
        next_index = self._current_index + 1
        if 0 <= next_index < len(self._results):
            self._play_at(next_index)
        else:
            self._player_ctrl.update_status(False, "Playback finished")
            self._current_index = -1
            self._current_title = ""

    # ------------------------------------------------------------------
    # Actions (keyboard bindings)
    # ------------------------------------------------------------------

    def action_toggle_pause(self) -> None:
        if self.player.process:
            self.player.toggle_pause()
            self._player_ctrl.update_status(
                self.player.is_playing, self._current_title
            )

    def action_seek_forward(self) -> None:
        if self.player.process:
            self.player.seek(5)

    def action_seek_backward(self) -> None:
        if self.player.process:
            self.player.seek(-5)

    def action_volume_up(self) -> None:
        if self.player.process:
            self.player.volume_up()
            self.notify("Volume +5", title="Volume", timeout=1)

    def action_volume_down(self) -> None:
        if self.player.process:
            self.player.volume_down()
            self.notify("Volume -5", title="Volume", timeout=1)

    def action_next_track(self) -> None:
        if self._results and self._current_index < len(self._results) - 1:
            self._play_at(self._current_index + 1)

    def action_prev_track(self) -> None:
        if self._results and self._current_index > 0:
            self._play_at(self._current_index - 1)


if __name__ == "__main__":
    app = YouTubePlayerApp()
    app.run()
