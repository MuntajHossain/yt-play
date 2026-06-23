import logging
import os
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("yt-play")

# Ensure libmpv DLL is findable
os.environ["PATH"] = os.path.dirname(os.path.abspath(__file__)) + os.pathsep + os.environ.get("PATH", "")

import mpv


class MpvPlayer:
    """Controls an mpv instance via python-mpv (libmpv) bindings."""

    # If playback time hasn't advanced for this long while not paused and
    # not actively buffering-up-to-100%, we consider it stalled (e.g. seeked
    # past the end of the demuxer cache with no error reported by mpv).
    STALL_TIMEOUT_SECS = 6.0
    # Don't act on rapid-fire seeks individually; wait for the user to stop
    # pressing the key before checking the stream is actually progressing.
    SEEK_SETTLE_SECS = 1.0

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

        # Stall-watchdog state
        self._last_seek_time: float = 0.0
        self._last_progress_time: float = 0.0
        self._last_seen_pos: float = -1.0
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()
        self._stall_reported = False

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
                cache=True,
                demuxer_max_bytes="50MiB",
                demuxer_readahead_secs=60,
                cache_secs=60,
                log_handler=self._mpv_log_handler,
                loglevel="info",
            )
        except Exception as e:
            log.exception("Failed to create mpv player")
            self._notify_error(f"Failed to create mpv player: {e}")
            return

        player = self._player
        player.volume = 50

        self._last_seek_time = 0.0
        self._last_progress_time = time.monotonic()
        self._last_seen_pos = -1.0
        self._stall_reported = False

        # Register property observers for time / duration
        @player.property_observer("time-pos")
        def _on_time_pos(_name, value):
            if value is not None:
                self.current_time = float(value)
                if self.current_time != self._last_seen_pos:
                    self._last_seen_pos = self.current_time
                    self._last_progress_time = time.monotonic()
                    self._stall_reported = False
                dur = self.duration
                if self.on_time_update:
                    self.on_time_update(self.current_time, dur)

        @player.property_observer("duration")
        def _on_duration(_name, value):
            if value is not None:
                self.duration = float(value)

        # Log seek-related mpv events for diagnostics
        @player.event_callback("seek")
        def _on_seek_event(event):
            log.info("MPV EVENT seek: pos=%.1f cache=%s", self.current_time, self._cache_snapshot())

        @player.event_callback("playback-restart")
        def _on_playback_restart(event):
            log.info("MPV EVENT playback-restart: pos=%.1f cache=%s", self.current_time, self._cache_snapshot())

        # Log demuxer cache underrun (paused-for-cache) changes
        @player.property_observer("paused-for-cache")
        def _on_paused_for_cache(_name, value):
            log.info("MPV cache underrun (paused-for-cache=%s) at pos=%.1f cache=%s",
                      value, self.current_time, self._cache_snapshot())

        # Route mpv's own log messages (warn and above) into our log file
        # is handled via the log_handler kwarg passed to mpv.MPV() above.

        # Register end-file event
        @player.event_callback("end-file")
        def _on_end_file(event):
            self.is_playing = False
            error_msg: Optional[str] = None
            reason = getattr(event, "reason", None)
            error_code = getattr(event, "error", None)
            log.info(
                "MPV EVENT end-file: reason=%s error=%s pos=%.1f cache=%s",
                reason, error_code, self.current_time, self._cache_snapshot(),
            )
            # event.reason: 0=EOF, 1=STOP, 2=QUIT, 3=ERROR
            if hasattr(event, "error") and event.error != 0:
                error_msg = f"mpv error code {event.error}"
            elif hasattr(event, "reason") and event.reason == 3:
                error_msg = "Stream disconnected (seek past buffered range?)"
            log.info("end-file resolved as: %s", error_msg if error_msg else "normal end (no error)")
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
            return

        self._start_watchdog()

    def toggle_pause(self):
        if self._player:
            self._player.pause = not self._player.pause
            self.is_playing = not self._player.pause

    def seek(self, seconds: float):
        if not self._player:
            log.warning("SEEK relative %.1fs ignored: no active player", seconds)
            return
        pre_pos = self.current_time
        pre_dur = self.duration
        cache_info = self._cache_snapshot()
        log.info(
            "SEEK relative requested: delta=%.1fs pos=%.1f dur=%.1f cache=%s",
            seconds, pre_pos, pre_dur, cache_info,
        )
        self._last_seek_time = time.monotonic()
        try:
            self._player.seek(seconds, reference="relative")
            log.info("SEEK relative call returned OK (delta=%.1fs from pos=%.1f)", seconds, pre_pos)
        except Exception:
            log.exception("SEEK relative call raised an exception (delta=%.1fs from pos=%.1f)", seconds, pre_pos)

    def seek_absolute(self, position: float):
        """Seek to an absolute time position (seconds)."""
        if not self._player:
            log.warning("SEEK absolute %.1fs ignored: no active player", position)
            return
        pre_pos = self.current_time
        cache_info = self._cache_snapshot()
        log.info(
            "SEEK absolute requested: target=%.1fs current_pos=%.1f cache=%s",
            position, pre_pos, cache_info,
        )
        self._last_seek_time = time.monotonic()
        try:
            self._player.seek(position, reference="absolute")
            log.info("SEEK absolute call returned OK (target=%.1fs)", position)
        except Exception:
            log.exception("SEEK absolute call raised an exception (target=%.1fs)", position)

    def _cache_snapshot(self) -> str:
        """Best-effort snapshot of mpv cache/demuxer state for diagnostics."""
        if not self._player:
            return "n/a"
        fields = [
            "cache-buffering-state",
            "demuxer-cache-time",
            "demuxer-cache-duration",
            "cache-speed",
            "paused-for-cache",
        ]
        parts = []
        for f in fields:
            try:
                parts.append(f"{f}={getattr(self._player, f.replace('-', '_'), None)}")
            except Exception:
                parts.append(f"{f}=<err>")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Stall watchdog
    # ------------------------------------------------------------------

    def _start_watchdog(self):
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="mpv-stall-watchdog", daemon=True
        )
        self._watchdog_thread.start()
        log.info("WATCHDOG started")

    def _stop_watchdog(self):
        self._watchdog_stop.set()
        t = self._watchdog_thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._watchdog_thread = None

    def _watchdog_loop(self):
        while not self._watchdog_stop.wait(1.0):
            player = self._player
            if not player:
                continue
            if not self.is_playing:
                continue
            try:
                paused = bool(player.pause)
            except Exception:
                paused = False
            if paused:
                continue

            now = time.monotonic()
            since_progress = now - self._last_progress_time
            since_seek = now - self._last_seek_time

            # Give mpv a grace period right after a seek before judging a stall,
            # and require the playback time to have genuinely stopped advancing.
            if since_seek < self.SEEK_SETTLE_SECS:
                continue
            if since_progress < self.STALL_TIMEOUT_SECS:
                continue
            if self._stall_reported:
                continue

            cache_info = self._cache_snapshot()
            log.warning(
                "WATCHDOG stall detected: pos=%.1f no progress for %.1fs (last seek %.1fs ago) cache=%s",
                self.current_time, since_progress, since_seek, cache_info,
            )
            self._stall_reported = True
            self.is_playing = False
            stuck_pos = self.current_time
            if self.on_end:
                self.on_end(f"Playback stalled at {stuck_pos:.1f}s (cache exhausted after seek)")


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

    def _mpv_log_handler(self, loglevel, component, message):
        """Receives raw libmpv log lines (cache/demuxer/network details)."""
        log.info("MPV LOG [%s/%s] %s", loglevel, component, message.rstrip() if hasattr(message, "rstrip") else message)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cleanup(self):
        self._stop_watchdog()
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