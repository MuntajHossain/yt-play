---
name: reference-run-tests-via-venv
description: uv run fails on this machine; run tests with .venv/Scripts/python -m pytest
metadata:
  node_type: memory
  type: reference
  originSessionId: 0ea3f9ed-57c1-4e17-a4b1-98a43f6bec1c
  modified: 2026-11-02T05:29:59.486Z
---

`uv run pytest` fails here with "uv trampoline failed to canonicalize script path" (seen 2026-11-02). Use `.venv/Scripts/python -m pytest -q` from the repo root instead — same suite, 179 tests, ~8s.

Debug playback/resume bugs from `log/yt-play-*.log` first (newest file = current session); the log traced every bug in the 2026-11-02 replay investigation. See [[feedback-caveman-ultra-always]] for reply style.
