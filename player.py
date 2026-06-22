import logging
import os
import threading
from typing import Callable, Optional

log = logging.getLogger("yt-play")

# Ensure libmpv DLL is findable
os.environ["PATH"] = os.path.dirname(os.path.abspath(__file__)) + os.pathsep + os.environ.get("PATH", "")

import mpv


class MpvPlayer:
    """Controls an mpv instance via python-mpv (libmpv) bindings."""

    def __init__(self):
        self._player: Optional[mpv.MPV] = None
        self._lock = threading.Lock()

        self.is_playing = False
        self.current_time = 0.0
        self.duration = 0.0
        self.title = ""

        # Callbacks – set these before calling play()
        self.on_time_update: Optional[Callable[[float, float], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None
        self.on_end: Optional[Callable[[Optional[str]], None]] = None
        # None = normal end (EOF/stop), str = error message for recovery

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def process(self) -> bool:
        """Whether a player instance is active."""
        return self._player is not None

    def play(self, url: str):
        """Start playing *url* (audio-only) through a new mpv instance."""
        self.stop()

        try:
            self._player = mpv.MPV(
                video=False,
                input_default_bindings=False,
                input_vo_keyboard=False,
                osc=False,
                idle=True,
            )
        except Exception as e:
            log.exception("Failed to create mpv player")
            self._notify_error(f"Failed to create mpv player: {e}")
            return

        player = self._player
        player.volume = 50

        # Register property observers for time / duration
        @player.property_observer("time-pos")
        def _on_time_pos(_name, value):
            if value is not None:
                self.current_time = float(value)
                dur = self.duration
                if self.on_time_update:
                    self.on_time_update(self.current_time, dur)

        @player.property_observer("duration")
        def _on_duration(_name, value):
            if value is not None:
                self.duration = float(value)

        # Register end-file event
        @player.event_callback("end-file")
        def _on_end_file(event):
            self.is_playing = False
            error_msg: Optional[str] = None
            # event.reason: 0=EOF, 1=STOP, 2=QUIT, 3=ERROR
            if hasattr(event, "error") and event.error != 0:
                error_msg = f"mpv error code {event.error}"
            elif hasattr(event, "reason") and event.reason == 3:
                error_msg = "Stream disconnected (seek past buffered range?)"
            if self.on_end:
                self.on_end(error_msg)

        # Start playback
        try:
            player.play(url)
            self.is_playing = True
        except Exception as e:
            log.exception("Failed to start playback for URL: %.80s", url)
            self._notify_error(f"Failed to play: {e}")
            self._cleanup()

    def toggle_pause(self):
        if self._player:
            self._player.pause = not self._player.pause
            self.is_playing = not self._player.pause

    def seek(self, seconds: float):
        if self._player:
            self._player.seek(seconds, reference="relative")

    def seek_absolute(self, position: float):
        """Seek to an absolute time position (seconds)."""
        if self._player:
            self._player.seek(position, reference="absolute")

    @property
    def volume(self) -> int:
        return self._player.volume if self._player else 50

    def set_volume(self, volume: int):
        if self._player:
            self._player.volume = max(0, min(100, volume))

    def volume_up(self, delta: int = 5):
        if self._player:
            self._player.volume = min(100, self._player.volume + delta)

    def volume_down(self, delta: int = 5):
        if self._player:
            self._player.volume = max(0, self._player.volume - delta)

    @property
    def speed(self) -> float:
        return self._player.speed if self._player else 1.0

    def speed_up(self, delta: float = 0.25):
        if self._player:
            self._player.speed = min(3.0, self._player.speed + delta)

    def speed_down(self, delta: float = 0.25):
        if self._player:
            self._player.speed = max(0.25, self._player.speed - delta)

    def stop(self):
        """Terminate mpv and clean up."""
        self._cleanup()
        self.is_playing = False
        self.current_time = 0.0
        self.duration = 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cleanup(self):
        with self._lock:
            if self._player:
                try:
                    self._player.terminate()
                except Exception:
                    log.exception("Error during mpv terminate")
                self._player = None

    def _notify_error(self, msg: str):
        if self.on_error:
            self.on_error(msg)

    # ------------------------------------------------------------------
    # Cleanup on garbage collection
    # ------------------------------------------------------------------

    def __del__(self):
        self._cleanup()
