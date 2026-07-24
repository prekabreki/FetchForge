---
description: Testing this app locally -- the heartbeat watchdog kills the server mid-curl, and how to exercise the frontend JS headlessly (jsdom, no browser)
type: project
---

Two things that bite when testing FetchForge locally on the Linux box:

**1. The server self-exits ~30s after launch unless a tab pings `/heartbeat`.**
`_heartbeat_watchdog()` flips `should_exit` once no ping arrives for `_HEARTBEAT_TIMEOUT`
(30s), and `_last_heartbeat` is set at launch -- so a plain `python -m fetchforge &`
backgrounded for a curl smoke test dies out from under you after 30s. For endpoint smoke
tests, boot it and curl immediately (`/version` should read the current `APP_VERSION`;
cross-origin POST -> 403; `/shutdown-now` without
the `X-DLPR-Token` -> 403 -- only ever test that 403 path, never send a real token). For
anything longer, keep hitting `/heartbeat` in a loop or expect the watchdog to reap it.

**2. Exercising the frontend JS without a browser -> use jsdom.**
Run the real `fetchforge/index.html` `<script>` under **jsdom** with
`runScripts:'dangerously'`, driving the actual UI functions (`addToQueue`, `renderDlQueue`,
`toItemPayload`, `handleMsg`). jsdom has no canvas 2d context (the progress `SparkChart`
crashes) -- stub `HTMLCanvasElement.prototype.getContext` with a self-returning callable
Proxy, and make `window.fetch` a never-resolving promise so on-load fetches don't process
and throw on shape. `dlQueue`/`currentTuneMode`/`_queueProgress` are `let` (not on `window`); reach them
only through the functions. This is the fastest real-execution check of the single-file
frontend. See [[project-overview]].

**Do NOT reach for extract-the-`<script>`-and-`eval`-it instead of jsdom** — tried in PR #38
and it burns a debugging cycle on a JS scoping trap. `let`/`const` inside a *direct* `eval`
are scoped to the eval, so extracted functions close over their own copy of a `let` module
variable; the harness then assigns a *different* (implicit global) binding, the functions
never see it, and the tests fail while the app is fine. (`var` in a direct eval does hoist
into the enclosing scope, which is the workaround if you're already down that road.) The
failure mode that actually matters is the inverse: a harness that pokes globals directly can
look green while testing its own state instead of the app's. jsdom avoids the whole class.
A committed jsdom suite is tracked as issue #42.

Not covered by any of this: a real yt-dlp download + NVENC encode (network/cookies/GPU) --
tracked as a manual verification task in the issue tracker.
