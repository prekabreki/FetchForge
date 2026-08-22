"""The cp1252 decode boundary for tools/issue-ready.py and tools/session-start-context.py.

Ported from vibe-skills `init-workspace` (issues #34, #35, #36). These two scripts are
verbatim copies of that skill's templates -- keep them in sync rather than editing here.

The bug only exists in how the PARENT decodes a child's stdout bytes, so every test
below drives a REAL child process. Handing the code a pre-parsed dict never touches it.

  U+2014 em dash    -> E2 80 94, all defined in cp1252 -> silent MOJIBAKE
  U+1F50D magnifier -> F0 9F 94 8D, and 0x8D is UNDEFINED in cp1252 -> the reader
                       thread raises, r.stdout comes back None, and the caller reports
                       a FALSE ALL-CLEAR at returncode 0.
"""

import importlib
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ir = importlib.import_module("issue-ready")
ssc = importlib.import_module("session-start-context")

NON_ASCII = "em—dash 🔍 done"


def _emit(runner, text):
    """Run a child that writes `text` as UTF-8 bytes on stdout, through `runner`."""
    code = (
        "import sys;"
        "sys.stdout.buffer.write(%r);"
        "sys.stdout.buffer.flush()" % (text.encode("utf-8"),)
    )
    return runner([sys.executable, "-c", code])


class TestDecodeBoundary(unittest.TestCase):
    def test_undefined_cp1252_byte_does_not_null_out_issue_ready_stdout(self):
        r = _emit(ir.run, '[{"number":1,"title":"find 🔍 it"}]')
        self.assertEqual(r.returncode, 0)
        self.assertIsNotNone(r.stdout, "stdout must not be None -- that is the bug")
        self.assertEqual(json.loads(r.stdout)[0]["title"], "find 🔍 it")

    def test_hook_run_decodes_utf8_from_a_real_child(self):
        r = _emit(ssc.run, NON_ASCII)
        self.assertEqual(r.returncode, 0)
        self.assertIsNotNone(r.stdout)
        self.assertEqual(r.stdout, NON_ASCII)


class TestEmptyStdoutIsNotAnAllClear(unittest.TestCase):
    """An exit-0 gh with empty stdout means something ate the output."""

    def _main_with(self, stdout):
        def fake_run(cmd):
            if "repo" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "owner/name\n", "")
            return subprocess.CompletedProcess(cmd, 0, stdout, "")

        orig_run, orig_gh = ir.run, ir._gh
        ir.run, ir._gh = fake_run, lambda: "gh"
        try:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = ir.main([])
            return rc, out.getvalue(), err.getvalue()
        finally:
            ir.run, ir._gh = orig_run, orig_gh

    def test_empty_stdout_with_exit_zero_fails_loudly(self):
        rc, out, err = self._main_with("")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("No ready issues", out)
        self.assertTrue(err.strip(), "must say something on stderr")

    def test_legitimately_empty_tracker_still_succeeds(self):
        # Zero open issues returns "[]" -- not empty stdout, so still a clean report.
        rc, out, err = self._main_with("[]")
        self.assertEqual(rc, 0)
        self.assertIn("No ready issues", out)


if __name__ == "__main__":
    unittest.main()
