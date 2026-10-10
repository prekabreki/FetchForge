# Agent Instructions

This project tracks work as **GitHub Issues** (see the task-tracking section in `CLAUDE.md`).
Run `python tools/issue-ready.py` to see ready work, and `gh issue list` for the full set.
Knowledge that should persist across sessions goes in `.memories/` (one fact per file; the
pre-commit hook keeps `.memories/README.md` indexed).

## Project Layout

FetchForge is packaged as the `fetchforge` pip package (`pyproject.toml`) — there is no
root-level `server.py` or `index.html` to edit. Application code and `index.html` live under
`fetchforge/`: backend `fetchforge/server.py`, CLI entry point `fetchforge/cli.py` (the
`fetchforge` console script; also runnable as `python -m fetchforge` via
`fetchforge/__main__.py`), and first-run ffmpeg provisioning in `fetchforge/provision.py`.
Runtime state (`downloads/`, `logs/`, `cookies.txt`, `history.json`) is written to whatever
directory `fetchforge` is launched from (`STATE_DIR = Path.cwd()`), not the package
directory (`PKG_DIR`) — don't confuse the two when tracing a file path. Test suite:
`.venv/bin/python -m unittest discover -s tests -v`. See `CLAUDE.md` for the full
architecture (endpoints, SSE events, encode-parameter logic).

## Quick Reference

```bash
python tools/issue-ready.py        # Ready work (open issues, dependencies satisfied)
gh issue list                      # All open issues
gh issue view <n>                  # View an issue
gh issue create --label bug,P1     # File a new issue
gh issue edit <n> --add-label in-progress   # Claim / mark in progress
gh issue close <n>                 # Complete work
```

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

<!-- init-workspace:start -->
## Task tracking & work environment

This repo tracks work with **GitHub Issues + the `gh` CLI**, and keeps durable project
knowledge in **`.memories/`** (grep-friendly markdown). This section is managed by the
`init-workspace` skill -- edit between the sentinels, or re-run the skill to refresh it.
Codebase/architecture docs belong elsewhere in AGENTS.md (run `/init` for those).

### Issues

```bash
gh issue list --state open                       # all open
gh issue list --state open --assignee @me        # your in-progress work
python tools/issue-ready.py                        # ready: open, unclaimed, unblocked, not held
gh issue view <N> --json state,title,body,comments
gh issue edit <N> --add-assignee @me              # claim (assignment is the lock)
gh issue edit <N> --add-label deferred            # park: real backlog, not actionable yet
gh issue close <N> --comment "<reason>"
```

- **Creating issues:** use the `gh-issues-writing` skill. Every issue carries testable
  acceptance criteria and a `How to verify` command that has actually been run, and the
  body goes in via `--body-file`, never inline `--body`. Never pass `--assignee` at
  creation: assignment is the claim, so it hides the issue from the ready view.
- **Not for human follow-ups.** "Ask X", "confirm with Y", "decide Z" go in the reply or
  the handoff, never into an issue.
- **Labels:** `P0`-`P4` priority; `bug`/`task`/`chore`/`epic`/`feature` type; `in-progress`
  and `deferred` status flags. Use only labels the repo has; never invent one.
- **Dependencies:** one marker per line, opening the line, no colon: `Blocked by #12, #15`
  / `Blocks #20`. A mid-sentence mention is deliberately ignored. `issue-ready.py` hides
  anything blocked by an open issue.
- **`deferred`** parks work that is real but *not actionable yet* -- data-gated, or waiting on an
  open design call. Carry the reason and a "revisit when ..." line in the body. A `deferred`
  issue still **blocks** its dependents. If the repo is foreman-onboarded, `needs-human`
  and `needs-replan` hold an issue the same way, and an `in-progress` label counts as a claim.
- Use `gh` for task tracking -- not TodoWrite or markdown TODO lists.

### Memories (`.memories/`)

Durable, grep-friendly project knowledge -- one fact per file, committed and shared.

- Each memory is `.memories/<kebab-key>.md`, opening with YAML frontmatter: `description:`
  (one line, required -- feeds the index) and `type:` (optional, free-form).
- `.memories/README.md` is an **auto-generated index** -- never hand-edit it. Add or change
  a memory, then commit: the pre-commit hook runs `tools/memory-index.py` and stages the
  refreshed index. Run it by hand any time.
- Save a memory **when the fact is learned**, not at wrap-up: one that cost real effort and
  isn't obvious from the code or git history. One fact, one file. Link related memories
  with `[[other-key]]`.
- `.memories/` is for facts about **this repo**. A cross-machine fact (a tool or CLI gotcha,
  an environment quirk, a workflow) goes to the synced `vibe-skills` `global-memory/` store.

### Sessions

Start with `/letsgo`, which orients against live issue state rather than the last
session's notes. End with `/call-it`, which files follow-ups, saves memories, and pushes.
Without those skills, the minimum is:

1. Every unfinished thread ends with an issue number or a one-line reason it has none.
2. Run quality gates if code changed (tests, linters, build).
3. Close finished issues; un-claim what you did not finish.
4. `git pull --rebase` then `git push`; confirm `git status` is clean. Work is not done
   until the push succeeds.
<!-- init-workspace:end -->
