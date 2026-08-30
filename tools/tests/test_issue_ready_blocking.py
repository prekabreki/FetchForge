"""Tests for issue-ready.py blocking detection forms.

Tests that the parser correctly detects:
1. Inline form: Blocked by #N
2. Colon form: Blocked by: #N
3. List form under ## Blocked by heading with - #N items
4. Comma-separated lists: Blocked by #1, #2, #3
5. NOT false positives like "part of epic #18" or "Blocks #30"
"""

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importlib

ir = importlib.import_module("issue-ready")
blocked_by = ir.blocked_by
is_blocked = ir.is_blocked


class TestBlockedByForms(unittest.TestCase):
    """Test all documented 'Blocked by' forms."""

    def test_inline_form_single_blocker(self):
        """Blocked by #12 (inline, no colon)."""
        issue = {"number": 41, "body": "Fix the bug.\nBlocked by #12."}
        self.assertEqual(blocked_by(issue), {12})

    def test_colon_form_single_blocker(self):
        """Blocked by: #12 (colon variant)."""
        issue = {"number": 41, "body": "Fix the bug.\nBlocked by: #12."}
        self.assertEqual(blocked_by(issue), {12})

    def test_comma_separated_list(self):
        """Blocked by #12, #13, #14 (comma list)."""
        issue = {"number": 41, "body": "Fix the bug.\nBlocked by #12, #13, #14."}
        self.assertEqual(blocked_by(issue), {12, 13, 14})

    def test_blocked_by_heading_with_list_items(self):
        """## Blocked by\n- #12\n- #13"""
        issue = {
            "number": 41,
            "body": "## Blocked by\n- #12\n- #13\n- #14",
        }
        self.assertEqual(blocked_by(issue), {12, 13, 14})

    def test_mixed_indentation_and_bullets(self):
        """Indented list items and different bullet styles."""
        issue = {
            "number": 41,
            "body": (
                "## Blocked by\n"
                "  - #12\n"
                "  * #13\n"
                "    + #14\n"
                "      1. #15\n"
            ),
        }
        self.assertEqual(blocked_by(issue), {12, 13, 14, 15})

    def test_no_false_positive_part_of_epic(self):
        """part of epic #18 should NOT be a blocker."""
        issue = {"number": 41, "body": "Part of epic #18."}
        self.assertEqual(blocked_by(issue), set())

    def test_no_false_positive_blocks(self):
        """Blocks #30 should NOT be a blocker (this issue blocks that one)."""
        issue = {"number": 41, "body": "Blocks #30."}
        self.assertEqual(blocked_by(issue), set())

    def test_only_leading_refs_count(self):
        """Only leading '#N' refs count, not trailing."""
        issue = {
            "number": 41,
            "body": "Blocked by #12 (needs #13). Part of #15.",
        }
        self.assertEqual(blocked_by(issue), {12})

    def test_issue_is_blocked_when_blocker_is_open(self):
        """is_blocked returns True when blocker is open."""
        issue = {"number": 41, "body": "Blocked by #12."}
        self.assertTrue(is_blocked(issue, {12}))

    def test_issue_is_not_blocked_when_blocker_is_closed(self):
        """is_blocked returns False when blocker is closed."""
        issue = {"number": 41, "body": "Blocked by #12."}
        self.assertFalse(is_blocked(issue, set()))  # no open blockers


if __name__ == "__main__":
    unittest.main()
