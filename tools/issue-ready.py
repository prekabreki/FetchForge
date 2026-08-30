"""The 'ready work' view (a 'bd ready' descendant). Cross-platform (Windows + Linux).

Prints unassigned, open GitHub issues NOT blocked by any other open issue and NOT parked
behind a hold label, grouped by priority label (P0 -> P4 -> none). Reads 'Blocked by #N'
(or 'Blocked by: #N', and every #N on the line for a comma-separated list) from bodies.

`deferred` is the hold label: real backlog that is not actionable YET -- data-gated, or
waiting on an open design call. GitHub has only open/closed, so without a hold the same
known-unactionable issues are offered every session and get re-triaged forever. (This is
the one thing beads' `deferred` status did that open/closed cannot express.)
"""

import argparse
import json
import re
import shutil
import subprocess
import sys

PRIORITIES = ["P0", "P1", "P2", "P3", "P4", "P?"]
TYPE_LABELS = {"bug", "task", "chore", "epic", "feature"}
# Labels that park an issue outside the ready view even when open and unassigned.
HOLD_LABELS = {"deferred"}
# A 'Blocked by' DECLARATION, which must OPEN its line -- after at most 3 spaces of
# indent, an optional list bullet ('-', '*', '+', or '1.'/'1)'), and optional bold. That
# is the documented convention: gh-issues-writing's body template emits
# '- Blocked by: #NN'. The colon is optional because both forms are in the wild.
#
# Matching 'Blocked by' ANYWHERE on the line (the previous shape) let prose ABOUT
# blockers declare blockers. A body whose text read
#   2. **Comma lists** `Blocked by #53, #52`
# marked ITSELF blocked by the two issues it was describing and sat out of the ready
# list for two days; an acceptance criterion quoting the syntax does the same.
BLOCKED_BY_LINE_RE = re.compile(
    r"(?im)^[ \t]{0,3}(?:[-*+][ \t]+|\d+[.)][ \t]+)?"   # optional list bullet
    r"(?:\*\*|__)?[ \t]*"                                # optional bold open
    r"Blocked by[ \t]*:?[ \t]*"                          # the marker itself
    r"(?:\*\*|__)?"                                      # optional bold close
    r"(.*)$"
)
# The ref list is a PREFIX of what follows the marker, not every '#N' on the line.
# Taking the whole line made a trailing reference a dependency:
#   Blocked by #12 (needs the client + the auth spike). Part of #15.
# recorded the open epic #15 as a blocker. So consume '#N' tokens while list
# punctuation separates them, and stop at the first token that is not one.
# No '^' anchors: these are used with .match(s, pos), and '^' would still only match
# at the real start of the string, so every ref after the first would be dropped.
LEADING_REF_RE = re.compile(r"[ \t]*#(\d+)")
REF_SEPARATOR_RE = re.compile(r"[ \t]*(?:[,;/&+]|and\b)[ \t]*")
# Whitespace alone continues the list ('#1 #2') but only when a ref really follows.
REF_GAP_RE = re.compile(r"[ \t]+(?=#\d)")


def _prefix_refs(tail):
    """Issue numbers in the leading '#N (sep #N)*' run of `tail`.

    Both rules still err toward HIDING work rather than advertising work that is not
    ready, which is the property the line-scoped shape was chosen for.
    """
    refs, pos = set(), 0
    while True:
        m = LEADING_REF_RE.match(tail, pos)
        if not m:
            return refs
        refs.add(int(m.group(1)))
        pos = m.end()
        step = REF_SEPARATOR_RE.match(tail, pos) or REF_GAP_RE.match(tail, pos)
        if not step:
            return refs
        pos = step.end()


def run(cmd):
    """subprocess.run for `gh`, decoded as UTF-8 rather than the locale codec.

    Windows python decodes child output with cp1252, which mangles gh's UTF-8 two
    different ways -- both silent, both at returncode 0:

      * bytes cp1252 happens to define become MOJIBAKE ('--' -> three characters);
      * bytes it does NOT define (0x81, 0x8D, 0x8F, 0x90, 0x9D -- reachable from
        common emoji, e.g. U+1F50D is F0 9F 94 8D) make the reader thread raise
        UnicodeDecodeError, so `r.stdout` comes back None and the caller sees no
        issues at all.

    The second mode is how a SessionStart hook reported "No ready issues" for weeks
    while 20 were ready. gh always emits UTF-8, so say so. errors="replace", never
    "strict": a mangled byte must cost one character, not take down the hook.
    """
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def priority_label(issue):
    for lab in issue.get("labels", []):
        if re.match(r"^P[0-4]$", lab.get("name", "")):
            return lab["name"]
    return "P?"


def blocked_by(issue):
    """Issue numbers declared by a 'Blocked by' line that OPENS its line.

    Only the leading run of '#N' refs counts -- see BLOCKED_BY_LINE_RE and
    _prefix_refs for the two false positives each rule was paid for.
    """
    body = issue.get("body") or ""
    refs = set()
    
    # First, find all 'Blocked by' lines (inline form)
    for line in BLOCKED_BY_LINE_RE.finditer(body):
        refs |= _prefix_refs(line.group(1))
    
    # Second, check for '## Blocked by' heading followed by list items
    blocked_by_heading_re = re.compile(r"(?im)^#{1,2}[ \t]*Blocked by[ \t]*$")
    for match in blocked_by_heading_re.finditer(body):
        section_start = match.end()
        # Find the next heading (starts with #) after this one
        remaining_text = body[section_start:]
        next_heading = re.search(r"(?im)\n[ \t]*#{1,2}", remaining_text)
        section_end = section_start + (next_heading.start() if next_heading else len(remaining_text))
        
        # Scan lines in the section for list items with issue references
        section_lines = body[section_start:section_end].split('\n')
        for line in section_lines:
            # Match list bullets followed by #N (including numbered lists)
            list_item_re = re.compile(r"^[ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+)#(\d+)")
            m = list_item_re.match(line)
            if m:
                refs.add(int(m.group(1)))
    
    return refs


def is_blocked(issue, open_numbers):
    return bool(blocked_by(issue) & open_numbers)


def is_held(issue):
    """True if a hold label (e.g. `deferred`) parks this issue out of ready."""
    return any(lab.get("name") in HOLD_LABELS for lab in issue.get("labels", []))


def filter_ready(issues):
    """Return unassigned, unheld issues not blocked by an open issue.

    `issues` MUST be the full list of open issues — the blocked-by check derives
    the open-issue number set from it, so a filtered subset under-detects blocks.
    Held issues stay in that set on purpose: a `deferred` blocker still blocks.
    """
    open_numbers = {int(i["number"]) for i in issues}
    ready = []
    for i in issues:
        if i.get("assignees"):
            continue
        if is_held(i):
            continue
        if is_blocked(i, open_numbers):
            continue
        ready.append(i)
    return ready


def format_output(ready):
    if not ready:
        return (
            "No ready issues. (All open issues are claimed, deferred, "
            "or blocked by another open issue.)"
        )
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
            types = [lab["name"] for lab in i.get("labels", []) if lab.get("name") in TYPE_LABELS]
            type_str = f"[{','.join(types)}] " if types else ""
            title = i.get("title", "")
            if len(title) > 80:
                title = title[:77] + "..."
            out.append(f"  #{i['number']:<4} {type_str}{title}")
    return "\n".join(out)


def _gh():
    if shutil.which("gh"):
        return "gh"
    print(
        "gh CLI not found. Install it and authenticate (gh auth login).",
        file=sys.stderr,
    )
    raise SystemExit(1)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Show ready (unassigned, unblocked) GitHub issues")
    ap.add_argument("--repo")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args(argv)

    # Titles and bodies carry em dashes and emoji, and this script's stdout is piped
    # into a SessionStart hook. A cp1252 stdout would raise on the way OUT -- the
    # mirror image of the decode bug run() fixes.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    gh = _gh()

    repo = args.repo
    if not repo:
        r = run([gh, "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
        repo = (r.stdout or "").strip()
        if r.returncode != 0 or not repo:
            print(
                "Could not determine the GitHub repo. Run inside a repo with a "
                "GitHub remote, or pass --repo owner/name.",
                file=sys.stderr,
            )
            return 1

    r = run(
        [
            gh,
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(args.limit),
            "--json",
            "number,title,assignees,labels,body",
        ]
    )
    if r.returncode != 0:
        print(
            f"gh issue list failed (exit {r.returncode}): {(r.stderr or '').strip()}",
            file=sys.stderr,
        )
        return 1
    if not (r.stdout or "").strip():
        # Never `json.loads(r.stdout or "[]")` here. An empty payload from an exit-0
        # gh means something ate the output, and printing "No ready issues" would
        # launder that crash into a false all-clear -- the actual bug this guards.
        # A repo with zero open issues returns "[]", which is NOT empty, so the
        # legitimate all-clear still goes through below.
        print(
            "gh issue list returned no output at exit 0 -- cannot tell ready work "
            "from none. (Suspect a decode failure or a truncated response.)",
            file=sys.stderr,
        )
        return 1

    issues = json.loads(r.stdout)
    print(format_output(filter_ready(issues)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
