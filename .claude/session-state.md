---
repo: FetchForge
updated: 2026-10-07
open_issues: [78, 80]
in_flight: []
blocked_on: []
---

Session tested handing scoped issues to DeepSeek without Claude in the loop, as a dry run for life after Claude Max.
foreman-dispatch works under auto mode: #79 merged via PR #82 (c190bb7); #78's PR #81 was bounced for a vacuous test and the issue re-scoped in place (labels scoped+bounced, awaiting promotion to ready-for-agent).
Root cause of the early failures: DeepSeek renamed deepseek-v4-flash to deepseek-flash; .foreman.local (gitignored) now uses deepseek/deepseek-flash.
The lighter `ds-run` launcher (vibe-skills tools/ds-run) works when run by hand but auto mode's classifier blocks Claude from launching it.
