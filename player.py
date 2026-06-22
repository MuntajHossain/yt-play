import json
import subprocess
import threading
import time
import os
import tempfile
import shutil
from typing import Callable, Optional


class MpvPlayer:
    """Controls an mpv subprocess via named-pipe / Unix-socket JSON IPC."""

    PIPE_POLL_INTERVAL = 0.1  # seconds between pipe-existence checks
    PIPE_POLL_MAX = 5.0       # total seconds to wait before giving up

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None

        if os.name == "nt":
            self.pipe_name = r"\\.\pipe\mpv-yt-play-pipe"
        else:
            self.pipe_name = os.path.join(
                tempfile.gettempdir(), "mpv-yt-play.sock"
            )

        self.is_playing = False
        self.current_time = 0.0
        self.duration = 0.0
        self.title = ""

        self._running = False           # signals the status thread to stop
        self._pipe_file = None          # file-like object for IPC
        self._pipe_lock = threading.Lock()
        self._status_thread: Optional[threading.Thread] = None

        # Callbacks – set these before calling play()
        self.on_time_update: Optional[Callable[[float, float], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None
        self.on_end: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def play(self, url: str):
        """Start playing *url* (audio-only) through a new mpv subprocess."""
        self.stop()

        if not shutil.which("mpv"):
            self._notify_error("mpv is not installed or not in PATH.")
            return

        # Clean up stale pipe from previous runs
        if os.name != "nt" and os.path.exists(self.pipe_name):
            try:
                os.remove(self.pipe_name)
            except OSError:
                pass

        cmd = [
            "mpv",
            "--no-video",
            f"--input-ipc-server={self.pipe_name}",
            "--idle=yes",
            url,
        ]

        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        self.process = subprocess.Popen(
            cmd,
            startupinfo=startupinfo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        self.is_playing = True
        self._running = True

        # Poll for the pipe to appear instead of a blind sleep
        if not self._wait_for_pipe():
            self._notify_error("mpv started but IPC pipe did not appear.")
            self.stop()
            return

        # Open the pipe
        try:
            if os.name == "nt":
                self._pipe_file = open(self.pipe_name, "w+t", encoding="utf-8")
            else:
                import socket

                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self.pipe_name)
                self._pipe_file = sock.makefile("rw", encoding="utf-8")

            # Ask mpv to push time / duration changes automatically
            self._send_command(["observe_property", 1, "playback-time"])
            self._send_command(["observe_property", 2, "duration"])

            # Start the reader thread (non-daemon so stop() can join it)
            self._status_thread = threading.Thread(
                target=self._status_loop, daemon=False
            )
            self._status_thread.start()

        except Exception as e:
            self._notify_error(f"IPC connection failed: {e}")
            self.stop()

    def toggle_pause(self):
        self._send_command(["cycle", "pause"])
        self.is_playing = not self.is_playing

    def seek(self, seconds: float):
        self._send_command(["seek", str(seconds)])

    def set_volume(self, volume: int):
        """Set volume (0–100)."""
        self._send_command(["set", "volume", max(0, min(100, volume))])

    def volume_up(self, delta: int = 5):
        self._send_command(["add", "volume", str(delta)])

    def volume_down(self, delta: int = 5):
        self._send_command(["add", "volume", str(-delta)])

    def stop(self):
        """Terminate mpv, close the pipe, and wait for the reader thread."""
        self._running = False

        # Close the pipe so the status thread unblocks
        with self._pipe_lock:
            if self._pipe_file:
                try:
                    self._pipe_file.close()
                except Exception:
                    pass
                self._pipe_file = None

        # Join the reader thread before killing the process
        if self._status_thread and self._status_thread.is_alive():
            self._status_thread.join(timeout=3)

        # Kill the subprocess
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

        self.is_playing = False
        self.current_time = 0.0
        self.duration = 0.0
        self._status_thread = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _wait_for_pipe(self) -> bool:
        """Block until the IPC pipe/socket exists.  Returns False on timeout."""
        elapsed = 0.0
        while elapsed < self.PIPE_POLL_MAX:
            if os.name == "nt":
                if os.path.exists(self.pipe_name):
                    return True
            else:
                if os.path.exists(self.pipe_name):
                    return True
            time.sleep(self.PIPE_POLL_INTERVAL)
            elapsed += self.PIPE_POLL_INTERVAL
        return False

    def _send_command(self, command: list):
        with self._pipe_lock:
            if self._pipe_file and not self._pipe_file.closed:
                try:
                    cmd_json = json.dumps({"command": command}) + "\n"
                    self._pipe_file.write(cmd_json)
                    self._pipe_file.flush()
                except Exception:
                    pass

    def _notify_error(self, msg: str):
        if self.on_error:
            self.on_error(msg)

    def _notify_end(self):
        if self.on_end:
            self.on_end()

    # ------------------------------------------------------------------
    # Reader thread – runs until stop() is called or the pipe breaks
    # ------------------------------------------------------------------

    def _status_loop(self):
        while self._running:
            pipe = self._pipe_file
            if not pipe or pipe.closed:
                break

            try:
                line = pipe.readline()
                if not line:
                    break

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if "event" not in data:
                    continue

                event = data["event"]

                if event == "end-file":
                    self.is_playing = False
                    self._notify_end()
                    break
                elif event == "property-change":
                    name = data.get("name")
                    value = data.get("data")
                    if value is not None:
                        if name == "playback-time":
                            self.current_time = float(value)
                        elif name == "duration":
                            self.duration = float(value)
                        if self.on_time_update:
                            self.on_time_update(
                                self.current_time, self.duration
                            )
            except Exception:
                break

        # Thread exiting – make sure state is clean
        self.is_playing = False
