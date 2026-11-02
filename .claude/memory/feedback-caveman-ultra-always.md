---
name: feedback-caveman-ultra-always
description: Every reply in this repo must use caveman skill at ultra level; hooks default to full but repo rule wins
metadata:
  node_type: memory
  type: feedback
  originSessionId: 0ea3f9ed-57c1-4e17-a4b1-98a43f6bec1c
  modified: 2026-11-02T05:25:32.746Z
---

Use the `caveman` skill in `ultra` mode for every reply in yt-play, including explanations, follow-ups, and wrap-ups.

**Why:** CLAUDE.md already required it, but I answered in normal prose and followed the SessionStart/UserPromptSubmit hooks, which default to `full`. User called this out on 2026-11-02 and asked to learn from it.

**How to apply:** run `/caveman ultra` at session start if the hook says `full`. Never drift to normal prose mid-session. Code, commits, PR text, and security warnings stay normal. Plugin source: https://github.com/JuliusBrussee/caveman, enabled via tracked `.claude/settings.json`.
