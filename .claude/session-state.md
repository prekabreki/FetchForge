---
repo: FetchForge
updated: 2026-10-07
open_issues: [78, 79]
in_flight: []
blocked_on: []
---

Session shipped port fallback (#75, 8765 then up to 8784, reusing a running FetchForge) and time-range capture (#76 via PR #77, v2.3.0).
Both open issues are yt-dlp resolution/update follow-ups found during #76 E2E, scoped and labelled `scoped` but not promoted to ready-for-agent.
The repo's .venv yt-dlp was hand-upgraded with uv to 2026.08.19 (stale 2026.07.04 gave YouTube 403s); #79 is what makes the in-app button do that.
