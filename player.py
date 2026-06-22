import asyncio
import json
import subprocess
import threading
import time
import os
import tempfile
import shutil
from typing import Callable, Optional

class MpvPlayer:
    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        
        if os.name == 'nt':
            self.pipe_name = r"\\.\pipe\mpv-yt-play-pipe"
        else:
            self.pipe_name = os.path.join(tempfile.gettempdir(), "mpv-yt-play.sock")
            
        self.is_playing = False
        self.current_time = 0.0
        self.duration = 0.0
        self.title = ""
        self._running = False
        self._pipe_file = None
        self._pipe_lock = threading.Lock()
        self._status_thread: Optional[threading.Thread] = None
        self.on_time_update: Optional[Callable[[float, float], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

    def play(self, url: str):
        self.stop()
        
        if not shutil.which("mpv"):
            if self.on_error:
                self.on_error("mpv is not installed or not in PATH.")
            return

        if os.name != 'nt' and os.path.exists(self.pipe_name):
            try:
                os.remove(self.pipe_name)
            except OSError:
                pass

        cmd = [
            "mpv",
            "--no-video",
            f"--input-ipc-server={self.pipe_name}",
            "--idle=yes",
            url
        ]
        
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        self.process = subprocess.Popen(
            cmd,
            startupinfo=startupinfo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        
        self.is_playing = True
        self._running = True
        
        # Wait for pipe/socket to be created
        time.sleep(1.0)
        
        try:
            if os.name == 'nt':
                self._pipe_file = open(self.pipe_name, "w+t", encoding="utf-8")
            else:
                import socket
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self.pipe_name)
                self._pipe_file = sock.makefile("rw", encoding="utf-8")
            
            # Observe properties so mpv pushes them automatically
            self._send_command(["observe_property", 1, "playback-time"])
            self._send_command(["observe_property", 2, "duration"])
            
            self._status_thread = threading.Thread(target=self._status_loop, daemon=True)
            self._status_thread.start()
        except Exception as e:
            if self.on_error:
                self.on_error(f"Error opening IPC: {e}")

    def _send_command(self, command: list):
        with self._pipe_lock:
            if self._pipe_file and not self._pipe_file.closed:
                try:
                    cmd_json = json.dumps({"command": command}) + "\n"
                    self._pipe_file.write(cmd_json)
                    self._pipe_file.flush()
                except Exception:
                    pass

    def toggle_pause(self):
        self._send_command(["cycle", "pause"])
        self.is_playing = not self.is_playing

    def seek(self, seconds: float):
        self._send_command(["seek", str(seconds)])

    def stop(self):
        self._running = False
        with self._pipe_lock:
            if self._pipe_file:
                try:
                    self._pipe_file.close()
                except:
                    pass
                self._pipe_file = None
            
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except:
                self.process.kill()
            self.process = None
            
        self.is_playing = False
        self.current_time = 0.0
        self.duration = 0.0

    def _status_loop(self):
        """Thread that reads mpv events from the IPC pipe"""
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
                    if "event" in data:
                        if data["event"] == "end-file":
                            self.is_playing = False
                        elif data["event"] == "property-change":
                            name = data.get("name")
                            value = data.get("data")
                            if value is not None:
                                if name == "playback-time":
                                    self.current_time = float(value)
                                elif name == "duration":
                                    self.duration = float(value)
                                
                                if self.on_time_update:
                                    self.on_time_update(self.current_time, self.duration)
                except json.JSONDecodeError:
                    pass
            except Exception:
                break
