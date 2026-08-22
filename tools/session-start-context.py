"""SessionStart hook output for Claude Code. Cross-platform (Windows + Linux).

Emits the contributor's open assignments and the priority-sorted ready-to-claim list as
hookSpecificOutput.additionalContext. Also self-heals the per-clone
core.hooksPath so the .memories index pre-commit hook is active for
everyone. Defensive: any failure is surfaced inside the context block
rather than thrown.
"""

import json
import subprocess
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent


def run(cmd):
    """subprocess.run decoded as UTF-8 rather than the locale codec.

    Windows python would otherwise decode child output with cp1252: issue titles and
    bodies carrying an em dash mojibake, and ones carrying a byte cp1252 leaves
    undefined (0x81, 0x8D, 0x8F, 0x90, 0x9D -- reachable from common emoji) make the
    reader thread raise, so `.stdout` comes back None at returncode 0. In a hook that
    reads as "you have no work". errors="replace" keeps a bad byte to one character.
    """
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def _self_heal_hookspath():
    try:
        if (REPO_ROOT / ".githooks").is_dir():
            git = ["git", "-C", str(REPO_ROOT), "config", "--local", "core.hooksPath"]
            cur = run(git)
            if cur.returncode != 0 or (cur.stdout or "").strip() != ".githooks":
                run(
                    [
                        "git",
                        "-C",
                        str(REPO_ROOT),
                        "config",
                        "--local",
                        "core.hooksPath",
                        ".githooks",
                    ]
                )
    except Exception:
        pass


def _assignments():
    try:
        r = run(["gh", "issue", "list", "--state", "open", "--assignee", "@me"])
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return out or "(no open issues assigned to you)"
    except Exception as e:
        return f"gh issue list failed: {e}"


def _ready():
    try:
        # sys.executable, not "python": under a hook the PATH python can be a
        # different interpreter than the one running this script, or absent.
        r = run([sys.executable, str(TOOLS_DIR / "issue-ready.py")])
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return out or "(issue-ready.py returned no output)"
    except Exception as e:
        return f"issue-ready.py failed: {e}"


def main():
    _self_heal_hookspath()
    ctx = (
        f"## Open issues assigned to you\n\n{_assignments()}\n\n"
        f"## Ready to claim\n\n{_ready()}"
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": ctx,
                }
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
