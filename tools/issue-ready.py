"""The 'ready work' view (a 'bd ready' descendant). Cross-platform (Windows + Linux).

Prints unassigned, open GitHub issues NOT blocked by any other open issue, grouped by
priority label (P0 -> P4 -> none). Reads 'Blocked by #N' from issue bodies.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys

PRIORITIES = ["P0", "P1", "P2", "P3", "P4", "P?"]
TYPE_LABELS = {"bug", "task", "chore", "epic", "feature"}


def run(cmd):
    """subprocess.run for `gh`, decoded as UTF-8.

    Windows python decodes child output with the locale codec (cp1252), which
    dies on any issue title/body containing an em dash or emoji: the reader
    thread raises, stdout comes back None, and the caller silently sees no
    issues. gh always emits UTF-8, so say so.
    """
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def priority_label(issue):
    for lab in issue.get("labels", []):
        if re.match(r"^P[0-4]$", lab.get("name", "")):
            return lab["name"]
    return "P?"


def is_blocked(issue, open_numbers):
    for m in re.finditer(r"(?i)Blocked by #(\d+)", issue.get("body") or ""):
        if int(m.group(1)) in open_numbers:
            return True
    return False


def filter_ready(issues):
    """Return unassigned issues not blocked by an open issue.

    `issues` MUST be the full list of open issues — the blocked-by check derives
    the open-issue number set from it, so passing a filtered subset under-detects blocks.
    """
    open_numbers = {int(i["number"]) for i in issues}
    ready = []
    for i in issues:
        if i.get("assignees"):
            continue
        if is_blocked(i, open_numbers):
            continue
        ready.append(i)
    return ready


def format_output(ready):
    if not ready:
        return "No ready issues. (All open issues are either claimed or blocked by another open issue.)"
    buckets = {p: [] for p in PRIORITIES}
    for i in ready:
        buckets[priority_label(i)].append(i)
    out = [f"Ready work - {len(ready)} issue(s) across priorities:"]
    for p in PRIORITIES:
        items = buckets[p]
        if not items:
            continue
        out.append("")
        out.append(f"[{p}] {len(items)} issue(s)")
        for i in items:
            types = [l["name"] for l in i.get("labels", []) if l.get("name") in TYPE_LABELS]
            type_str = f"[{','.join(types)}] " if types else ""
            title = i.get("title", "")
            if len(title) > 80:
                title = title[:77] + "..."
            out.append(f"  #{i['number']:<4} {type_str}{title}")
    return "\n".join(out)


def _gh():
    if shutil.which("gh"):
        return "gh"
    print("gh CLI not found. Install it and authenticate (gh auth login).", file=sys.stderr)
    raise SystemExit(1)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Show ready (unassigned, unblocked) GitHub issues")
    ap.add_argument("--repo")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args(argv)
    # Titles carry em dashes and emoji; a cp1252 stdout would raise on the way out
    # (the mirror image of the decode bug `run` fixes) when this runs under a pipe.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    gh = _gh()

    repo = args.repo
    if not repo:
        r = run([gh, "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
        repo = (r.stdout or "").strip()
        if r.returncode != 0 or not repo:
            print("Could not determine the GitHub repo. Run inside a repo with a GitHub remote, "
                  "or pass --repo owner/name.", file=sys.stderr)
            return 1

    r = run([gh, "issue", "list", "--repo", repo, "--state", "open",
             "--limit", str(args.limit), "--json", "number,title,assignees,labels,body"])
    if r.returncode != 0:
        print(f"gh issue list failed (exit {r.returncode}): {(r.stderr or '').strip()}", file=sys.stderr)
        return 1
    if not (r.stdout or "").strip():
        # Never fall back to "[]" here: an empty payload from an exit-0 gh means
        # something ate the output, and printing "No ready issues" would be a
        # false all-clear.
        print("gh issue list returned no output — cannot tell ready work from none.",
              file=sys.stderr)
        return 1

    issues = json.loads(r.stdout)
    print(format_output(filter_ready(issues)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
