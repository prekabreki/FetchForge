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
and throw on shape. `dlQueue`/`currentTuneMode` are `let` (not on `window`); reach them
only through the functions. This is the fastest real-execution check of the single-file
frontend. See [[project-overview]].

Not covered by any of this: a real yt-dlp download + NVENC encode (network/cookies/GPU) --
tracked as a manual verification task in the issue tracker.
