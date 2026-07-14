"""SessionStart hook output for Claude Code. Cross-platform (Windows + Linux).

Emits the contributor's open assignments and the priority-sorted ready-to-claim list as
hookSpecificOutput.additionalContext. Also self-heals the per-clone core.hooksPath so the
.memories index pre-commit hook is active for everyone. Defensive: any failure is surfaced
inside the context block rather than thrown.
"""
import json
import subprocess
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent


def _self_heal_hookspath():
    try:
        if (REPO_ROOT / ".githooks").is_dir():
            cur = subprocess.run(["git", "-C", str(REPO_ROOT), "config", "--local", "core.hooksPath"],
                                 capture_output=True, text=True)
            if cur.returncode != 0 or cur.stdout.strip() != ".githooks":
                subprocess.run(["git", "-C", str(REPO_ROOT), "config", "--local", "core.hooksPath", ".githooks"],
                               capture_output=True, text=True)
    except Exception:
        pass


def _assignments():
    try:
        r = subprocess.run(["gh", "issue", "list", "--state", "open", "--assignee", "@me"],
                           capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip()
        return out or "(no open issues assigned to you)"
    except Exception as e:
        return f"gh issue list failed: {e}"


def _ready():
    try:
        r = subprocess.run(["python", str(TOOLS_DIR / "issue-ready.py")],
                           capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip()
        return out or "(issue-ready.py returned no output)"
    except Exception as e:
        return f"issue-ready.py failed: {e}"


def main():
    _self_heal_hookspath()
    ctx = (f"## Open issues assigned to you\n\n{_assignments()}\n\n"
           f"## Ready to claim\n\n{_ready()}")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": ctx}}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
