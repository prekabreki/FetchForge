---
description: Deploying/merging/syncing while a FetchForge job runs -- the editable install serves index.html from disk per-request, so a page reload after a git file change mismatches the still-in-memory old backend
type: project
---

FetchForge is normally run as an editable install (`pip install -e .`), so `PKG_DIR`
points straight at the source tree and `GET /` re-reads `fetchforge/index.html` **from
disk on every request** (server.py:~950). `server.py` itself, by contrast, is loaded into
memory once at launch. Consequences when a download/encode job is running and you touch the
repo:

- **Git operations are safe for the live process.** Merging a PR remotely, or even
  `git checkout`/`pull` changing on-disk files, does NOT disturb the running server — the
  active SSE job keeps streaming against the in-memory backend. So "can we merge while I'm
  working in the server?" → yes.
- **The trap is a page reload.** After on-disk `index.html` changes (branch switch, pull,
  or just editing on the current branch), reloading the tab serves the NEW frontend to the
  OLD in-memory backend. New JS that calls an endpoint the running server doesn't have yet
  (e.g. a freshly added `/fetch-cookies`) 404s. Keep the tab open, don't reload, until you
  restart.
- **Backend changes need a restart to take effect;** frontend changes only need a reload
  (with the matching backend). The header `v X.Y.Z` + green `/version` dot is the "both are
  fresh" confirmation after restart.

Practical wrap-up move: do remote merge + local sync while a job runs, tell the user not to
reload, and restart `fetchforge` once the job finishes. See [[project-overview]] and
[[local-testing-gotchas]].
