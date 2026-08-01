"""Cookie-source scanning diagnostics (issues #43 / #46).

#46 asks the server to distinguish a Chromium profile that *cannot* be read
because of App-Bound Encryption (Chromium 127+, Windows-only, permanent) from a
profile that merely failed this time (locked DB, absent keyring). #43 relies on
every failed candidate keeping its own `error` string so the UI can show it.

Everything here uses temp dirs and synthetic `Local State` JSON — no real
browser profile is touched and no cookie value is ever read.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fetchforge import server


def _write_local_state(root: Path, payload) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    ls = root / "Local State"
    ls.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                  encoding="utf-8")
    return ls


class _TempRoot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "User Data"
        self.root.mkdir(parents=True)
        # Detection is Windows-only by definition; pin the platform so the suite
        # gives the same answer on the Linux CI box and the Windows dev box.
        p = mock.patch.object(server.platform, "system", return_value="Windows")
        p.start()
        self.addCleanup(p.stop)


class TestAppBoundEncryptionDetection(_TempRoot):

    def test_app_bound_key_present_is_detected(self):
        _write_local_state(self.root, {"os_crypt": {
            "encrypted_key": "RFBBUElsZWdhY3k=",
            "app_bound_encrypted_key": "QVBQQm91bmRLZXlCbG9i",
        }})
        self.assertTrue(server._chromium_uses_app_bound_encryption(self.root))

    def test_legacy_key_only_is_not_detected(self):
        # Pre-127 Chromium (and the Linux/macOS layout): DPAPI/keyring only.
        _write_local_state(self.root, {"os_crypt": {"encrypted_key": "RFBBUElsZWdhY3k="}})
        self.assertFalse(server._chromium_uses_app_bound_encryption(self.root))

    def test_empty_app_bound_key_is_not_detected(self):
        _write_local_state(self.root, {"os_crypt": {"app_bound_encrypted_key": ""}})
        self.assertFalse(server._chromium_uses_app_bound_encryption(self.root))

    def test_missing_local_state_returns_false(self):
        self.assertFalse(server._chromium_uses_app_bound_encryption(self.root))

    def test_empty_local_state_returns_false(self):
        _write_local_state(self.root, "")
        self.assertFalse(server._chromium_uses_app_bound_encryption(self.root))

    def test_invalid_json_returns_false(self):
        _write_local_state(self.root, "{not json at all")
        self.assertFalse(server._chromium_uses_app_bound_encryption(self.root))

    def test_non_object_json_returns_false(self):
        _write_local_state(self.root, "[1, 2, 3]")
        self.assertFalse(server._chromium_uses_app_bound_encryption(self.root))

    def test_non_object_os_crypt_returns_false(self):
        _write_local_state(self.root, {"os_crypt": "nope"})
        self.assertFalse(server._chromium_uses_app_bound_encryption(self.root))

    def test_unreadable_root_returns_false(self):
        # A root that is not even a directory must not raise out of the scan.
        self.assertFalse(server._chromium_uses_app_bound_encryption(
            self.root / "Local State" / "deeper"))

    def test_not_detected_off_windows(self):
        # ABE has no Linux/macOS equivalent — a Linux user with a legitimate
        # keyring failure must never be told their setup is impossible.
        _write_local_state(self.root, {"os_crypt": {"app_bound_encrypted_key": "x"}})
        for sysname in ("Linux", "Darwin"):
            with mock.patch.object(server.platform, "system", return_value=sysname):
                self.assertFalse(server._chromium_uses_app_bound_encryption(self.root))


class TestDecryptFailureClassification(unittest.TestCase):

    def test_dpapi_message_is_a_decrypt_failure(self):
        self.assertTrue(server._looks_like_decrypt_failure(
            "Failed to decrypt with DPAPI. See  "
            "https://github.com/yt-dlp/yt-dlp/issues/10927  for more info"))

    def test_locked_database_is_not_a_decrypt_failure(self):
        # yt-dlp issue 7271 — a genuinely different, possibly transient failure.
        self.assertFalse(server._looks_like_decrypt_failure(
            "Could not copy Chrome cookie database. See  "
            "https://github.com/yt-dlp/yt-dlp/issues/7271  for more info"))

    def test_none_and_empty_are_not_decrypt_failures(self):
        self.assertFalse(server._looks_like_decrypt_failure(None))
        self.assertFalse(server._looks_like_decrypt_failure(""))


class TestFailureReason(_TempRoot):

    def test_decrypt_failure_on_abe_profile_is_flagged(self):
        _write_local_state(self.root, {"os_crypt": {"app_bound_encrypted_key": "x"}})
        self.assertEqual(
            server._failure_reason(self.root, "Failed to decrypt with DPAPI."),
            server.APP_BOUND_ENCRYPTION)

    def test_locked_database_on_abe_profile_keeps_its_own_reason(self):
        # #46 danger zone: the ABE summary must not swallow a different failure.
        _write_local_state(self.root, {"os_crypt": {"app_bound_encrypted_key": "x"}})
        self.assertEqual(
            server._failure_reason(self.root, "Could not copy Chrome cookie database."),
            "")

    def test_decrypt_failure_without_abe_key_is_not_flagged(self):
        _write_local_state(self.root, {"os_crypt": {"encrypted_key": "x"}})
        self.assertEqual(
            server._failure_reason(self.root, "Failed to decrypt with DPAPI."), "")


class TestScanAnnotatesCandidates(unittest.TestCase):
    """`_scan_all_browser_cookies` must attach the reason without ever letting a
    detection problem escape the per-candidate isolation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        p = mock.patch.object(server.platform, "system", return_value="Windows")
        p.start()
        self.addCleanup(p.stop)
        # No Firefox root in these scans unless a test opts in.
        ff = mock.patch.object(server, "_firefox_root",
                               return_value=self.base / "no-such-firefox")
        ff.start()
        self.addCleanup(ff.stop)

    def _root(self, name, local_state):
        root = self.base / name
        _write_local_state(root, local_state)
        return root

    def test_abe_profile_is_flagged_and_locked_db_is_not(self):
        abe_root = self._root("brave", {"os_crypt": {"app_bound_encrypted_key": "x"}})
        plain_root = self._root("chrome", {"os_crypt": {"encrypted_key": "x"}})
        errors = {
            "brave": "Failed to decrypt with DPAPI. See  ...10927  for more info",
            "chrome": "Could not copy Chrome cookie database. See  ...7271  for more info",
        }

        def boom(browser, profile):
            raise RuntimeError(errors[browser])

        with mock.patch.object(server, "_browser_roots",
                               return_value={"brave": abe_root, "chrome": plain_root}), \
             mock.patch.object(server, "_extract_one", side_effect=boom):
            entries = [e for e, _ in server._scan_all_browser_cookies()]

        by_browser = {e["browser"]: e for e in entries}
        self.assertEqual(by_browser["brave"]["reason"], server.APP_BOUND_ENCRYPTION)
        self.assertNotIn("reason", by_browser["chrome"])
        # #43: every candidate keeps its own error string for the UI to show.
        self.assertIn("DPAPI", by_browser["brave"]["error"])
        self.assertIn("Could not copy", by_browser["chrome"]["error"])

    def test_successful_candidate_carries_no_reason(self):
        root = self._root("brave", {"os_crypt": {"app_bound_encrypted_key": "x"}})

        class _Jar(list):
            pass

        jar = _Jar()
        with mock.patch.object(server, "_browser_roots", return_value={"brave": root}), \
             mock.patch.object(server, "_extract_one", return_value=jar):
            entries = [e for e, _ in server._scan_all_browser_cookies()]

        self.assertEqual(len(entries), 1)
        self.assertNotIn("reason", entries[0])
        self.assertNotIn("error", entries[0])

    def test_malformed_local_state_does_not_break_the_scan(self):
        root = self._root("brave", "{{{ not json")
        with mock.patch.object(server, "_browser_roots", return_value={"brave": root}), \
             mock.patch.object(server, "_extract_one",
                               side_effect=RuntimeError("Failed to decrypt with DPAPI.")):
            entries = [e for e, _ in server._scan_all_browser_cookies()]

        self.assertEqual(len(entries), 1)
        self.assertNotIn("reason", entries[0])
        self.assertIn("DPAPI", entries[0]["error"])

    def test_firefox_is_never_flagged(self):
        ff_root = self.base / "firefox"
        ff_root.mkdir()
        with mock.patch.object(server, "_browser_roots", return_value={}), \
             mock.patch.object(server, "_firefox_root", return_value=ff_root), \
             mock.patch.object(server, "_extract_one",
                               side_effect=RuntimeError("Failed to decrypt with DPAPI.")):
            entries = [e for e, _ in server._scan_all_browser_cookies()]

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["browser"], "firefox")
        self.assertNotIn("reason", entries[0])

    def test_no_candidate_flagged_on_linux(self):
        root = self._root("brave", {"os_crypt": {"app_bound_encrypted_key": "x"}})
        with mock.patch.object(server.platform, "system", return_value="Linux"), \
             mock.patch.object(server, "_browser_roots", return_value={"brave": root}), \
             mock.patch.object(server, "_extract_one",
                               side_effect=RuntimeError("Failed to decrypt with DPAPI.")):
            entries = [e for e, _ in server._scan_all_browser_cookies()]

        self.assertEqual(len(entries), 1)
        self.assertNotIn("reason", entries[0])


if __name__ == "__main__":
    unittest.main()
