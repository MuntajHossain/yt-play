# Agents Configuration

This file outlines the configuration and details of the YouTube Player project for future AI development agents.

## Project Overview
A terminal-based YouTube player using `yt-dlp` to query and fetch audio streams, and `mpv` to handle audio playback. The interface is built with the Textual TUI framework.

## Tech Stack
* **Language:** Python
* **Package Manager:** uv
* **Key Dependencies:**
  * `yt-dlp` - For searching YouTube and retrieving stream URLs.
  * `textual` - For the terminal user interface.
  * `mpv` - External binary used for playing the audio streams.

## Project Structure
* [main.py](file:///c:/Users/hossa/Personal/Python/yt-play/main.py) - Textual Application coordinator and event handler.
* [search.py](file:///c:/Users/hossa/Personal\Python\yt-play\search.py) - Asynchronous wrappers for running `yt-dlp` queries.
* [player.py](file:///c:/Users/hossa\Personal\Python\yt-play\player.py) - Class to interact with the background `mpv` subprocess via Windows named pipes JSON IPC.
* [ui_components.py](file:///c:/Users/hossa\Personal\Python\yt-play\ui_components.py) - Custom Textual widgets for search inputs, result tables, and progress display.

## Setup & Installation
```bash
# Install dependencies using uv
uv pip install -r requirements.txt

# Or install directly
uv pip install yt-dlp textual mpv

# Ensure mpv is installed and in PATH
# Windows: Download from mpv.io or use winget
# macOS: brew install mpv
# Linux: apt install mpv
```

## Running the Application
```bash
uv run main.py
```

## Common Mistakes & Troubleshooting

### 1. mpv Not Found
**Problem:** `mpv: executable not found`
**Solution:** Ensure mpv is installed and in your system PATH. On Windows, download from mpv.io or use winget: `winget install mpv`

### 2. yt-dlp Fails to Fetch Streams
**Problem:** No video results or empty search
**Solution:** Check internet connection and yt-dlp version: `yt-dlp --version`. Update: `uv pip install --upgrade yt-dlp`

### 3. Named Pipe Communication Errors
**Problem:** IPC communication fails between player.py and mpv
**Solution:** Ensure mpv is running in background mode with JSON IPC enabled. Check Windows named pipes are accessible.

### 4. Terminal Audio Issues
**Problem:** No sound plays
**Solution:**
- Windows: Ensure default audio output device is selected
- Disable system audio enhancements in device properties
- Check mpv audio device: `mpv --audio-device-list`

### 5. Textual TUI Issues
**Problem:** Application crashes or UI doesn't render
**Solution:** Ensure Textual is installed and Python version is compatible (3.9+)

## Best Practices for AI Agents

### When Modifying Player Logic
1. **Always test mpv integration separately** before integrating into main.py
2. **Handle mpv subprocess cleanup** in finally blocks to avoid zombie processes
3. **Use async/await patterns** consistently throughout the codebase
4. **Test with actual YouTube URLs**, not mock data

### When Modifying UI Components
1. **Maintain Textual widget conventions** (on_mount, on_key, on_click handlers)
2. **Keep custom widgets self-contained** with clear separation of concerns
3. **Test keyboard navigation** in Textual's interactive mode before committing

### When Modifying Search Logic
1. **Use yt-dlp's built-in playlist handling** for multi-video queries
2. **Cache search results** to avoid excessive API calls
3. **Handle rate limiting** gracefully (yt-dlp has built-in delays)

### Important Notes
- **Windows-specific:** Named pipe IPC requires Windows APIs
- **Audio format:** mpv handles audio, not video. Only audio streams are fetched
- **Process management:** mpv runs as background subprocess, main process handles UI
- **Error handling:** All async operations must have try/except blocks

## Dependencies Reference

### Required
- `yt-dlp>=2023.11.16` - YouTube stream extraction
- `textual>=0.12.0` - Terminal UI framework
- `mpv-binary` - Audio playback (platform-specific installation)

### Optional (for enhanced features)
- `aiohttp` - Async HTTP requests
- `rich` - Enhanced terminal formatting

## Testing Commands
```bash
# Test yt-dlp connectivity
yt-dlp "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Test mpv installation
mpv --version

# Run application
uv run main.py
```

# Record common error here