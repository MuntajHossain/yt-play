from textual.app import App, ComposeResult
from textual.containers import Container, Vertical
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
        ("left", "seek_backward", "Seek -5s")
    ]

    def __init__(self):
        super().__init__()
        self.player = MpvPlayer()
        self.player.on_time_update = self.on_time_update
        self.player.on_error = self.on_player_error
        self.current_results = []
        self.current_title = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container():
            yield SearchWidget()
            yield ResultsWidget()
            yield PlayerControlWidget()
        yield Footer()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search_input":
            query = event.input.value
            if query:
                self.action_search(query)

    @work(exclusive=True)
    async def action_search(self, query: str) -> None:
        self.query_one("#results_title").update(f"Searching for '{query}'...")
        results = await search_youtube(query)
        self.current_results = results
        
        results_widget = self.query_one(ResultsWidget)
        results_widget.populate(results)
        
        count = len(results)
        self.query_one("#results_title").update(f"Search Results ({count})")

    @work(exclusive=True)
    async def play_video(self, index: int) -> None:
        if index < 0 or index >= len(self.current_results):
            return
            
        result = self.current_results[index]
        self.current_title = result.title
        
        # Extract audio URL
        self.query_one(PlayerControlWidget).update_status(False, f"Loading {result.title}...")
        
        audio_url = await extract_audio_url(result.url)
        
        if audio_url:
            self.player.play(audio_url)
            self.query_one(PlayerControlWidget).update_status(True, result.title)
        else:
            self.query_one(PlayerControlWidget).update_status(False, "Failed to load audio")

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "results_list":
            self.play_video(event.option_index)

    def on_time_update(self, current_time: float, duration: float) -> None:
        try:
            player_ctrl = self.query_one(PlayerControlWidget)
            self.call_from_thread(player_ctrl.update_progress, current_time, duration)
        except Exception:
            pass

    def on_player_error(self, message: str) -> None:
        self.call_from_thread(self.notify, message, title="Player Error", severity="error")

    def action_toggle_pause(self) -> None:
        if self.player.process:
            self.player.toggle_pause()
            player_ctrl = self.query_one(PlayerControlWidget)
            player_ctrl.update_status(self.player.is_playing, self.current_title)

    def action_seek_forward(self) -> None:
        if self.player.process:
            self.player.seek(5)

    def action_seek_backward(self) -> None:
        if self.player.process:
            self.player.seek(-5)

    def action_quit(self) -> None:
        self.player.stop()
        self.exit()

if __name__ == "__main__":
    app = YouTubePlayerApp()
    app.run()
