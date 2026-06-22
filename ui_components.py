from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, OptionList, ProgressBar, Label, Static
from textual.widgets.option_list import Option

class SearchWidget(Static):
    def compose(self) -> ComposeResult:
        yield Input(placeholder="Search YouTube...", id="search_input")
        yield Label("Press Enter to search", id="search_help")

class ResultsWidget(Static):
    def compose(self) -> ComposeResult:
        yield Label("Search Results", id="results_title")
        yield OptionList(id="results_list")
        
    def populate(self, results: list):
        option_list = self.query_one(OptionList)
        option_list.clear_options()
        for i, res in enumerate(results):
            # Formatted text for each option
            text = f"{res.title} [{res.duration_str}] - {res.uploader}"
            option_list.add_option(Option(text, id=f"result_{i}"))

class PlayerControlWidget(Static):
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Now Playing: Nothing", id="now_playing")
            with Horizontal(id="progress_container"):
                yield Label("00:00", id="time_current")
                yield ProgressBar(total=100, show_eta=False, id="progress_bar")
                yield Label("00:00", id="time_total")
            yield Label("Controls: [Space] Play/Pause | [Left/Right] Seek +/- 5s", id="controls_help")

    def update_status(self, is_playing: bool, title: str):
        label = self.query_one("#now_playing", Label)
        state = "▶" if is_playing else "⏸"
        if title:
            label.update(f"Now Playing: {state} {title}")
        else:
            label.update("Now Playing: Nothing")
            
    def update_progress(self, current_time: float, total_time: float):
        if total_time <= 0:
            return
            
        progress_bar = self.query_one(ProgressBar)
        progress_bar.update(progress=(current_time / total_time) * 100)
        
        current_lbl = self.query_one("#time_current", Label)
        total_lbl = self.query_one("#time_total", Label)
        
        current_lbl.update(self._format_time(current_time))
        total_lbl.update(self._format_time(total_time))
        
    def _format_time(self, seconds: float) -> str:
        seconds = int(seconds)
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"
