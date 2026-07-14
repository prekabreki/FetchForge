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
