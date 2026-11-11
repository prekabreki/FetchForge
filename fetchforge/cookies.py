"""Cookies: Netscape validation, stale-cookie detection, and best-effort
extraction from installed browsers (issues #9 / #12 / #43 / #46)."""
import json
import os
import platform
from pathlib import Path
from typing import Optional

from . import config

# Set when yt-dlp reports the loaded cookies have been rotated/invalidated. Once
# that happens the cookies are dead, and passing them is strictly worse than
# passing none (forces a degraded client + hard format failures), so we drop them
# for the rest of the run. Persists across the client's retry rounds; reset when
# the user loads fresh cookies (upload/paste), which deserve a new chance.
_cookies_disabled = False

# yt-dlp's stale-cookie warning, e.g. "The provided YouTube account cookies are no
# longer valid. They have likely been rotated in the browser as a security measure."
_STALE_COOKIE_MARKERS = ("no longer valid", "have likely been rotated")


def cookie_args() -> list:
    if _cookies_disabled:
        return []
    return ["--cookies", str(config.COOKIES_PATH)] if config.COOKIES_PATH.exists() else []


def _maybe_flag_stale_cookies(line: str) -> Optional[str]:
    """If a yt-dlp output line signals rotated/invalid cookies, disable cookies for
    the rest of the run and return a one-time user-facing warning to emit; else None."""
    global _cookies_disabled
    if _cookies_disabled or not config.COOKIES_PATH.exists():
        return None
    low = line.lower()
    if "cookie" in low and any(m in low for m in _STALE_COOKIE_MARKERS):
        _cookies_disabled = True
        return ("Your cookies appear stale (rotated/invalidated by the browser). "
                "Continuing this run without cookies — public videos still download. "
                "For private/age-restricted videos, re-export cookies from a fresh "
                "private window and reload them.")
    return None


def _validate_cookies_text(text: str) -> dict:
    """Parse a Netscape cookie file and report counts. Does not save."""
    text = (text or "").strip()
    if not text:
        return {"valid": False, "reason": "empty input"}

    lines = text.splitlines()
    cookie_lines = [l for l in lines if l.strip() and not l.lstrip().startswith("#")]
    if not cookie_lines:
        return {"valid": False, "reason": "no cookie entries found"}

    valid_cookies = 0
    youtube_cookies = 0
    for l in cookie_lines:
        parts = l.split("\t")
        if len(parts) >= 7:
            valid_cookies += 1
            domain = parts[0].lower()
            if "youtube" in domain or "google" in domain:
                youtube_cookies += 1

    if valid_cookies == 0:
        return {"valid": False, "reason": "lines are not tab-separated Netscape cookies"}

    return {"valid": True, "cookies": valid_cookies, "youtube_cookies": youtube_cookies}


# ── "Attempt to fetch cookies" — read cookies straight from installed browsers ─
# yt-dlp can decrypt a browser's cookie store directly (same path
# `--cookies-from-browser` uses). We scan every installed Chromium-family browser
# and ALL its profiles (the YouTube-logged-in one is often not "Default"), plus
# Firefox's default, count YouTube/Google cookies in each, and save the richest
# source. Best-effort by design (issue #12): a profile that errors (locked
# keyring/DB, no browser) is recorded and skipped, never fatal.
_CHROMIUM_BROWSERS = ("brave", "chrome", "chromium", "edge", "vivaldi", "opera")


def _browser_roots() -> dict:
    """browser_name → user-data root for the current OS. A Chromium root holds a
    `Local State` JSON and per-profile subdirs; a missing root = browser absent."""
    home = Path.home()
    sysname = platform.system()
    if sysname == "Windows":
        local = Path(os.getenv("LOCALAPPDATA", str(home / "AppData/Local")))
        roaming = Path(os.getenv("APPDATA", str(home / "AppData/Roaming")))
        return {
            "brave": local / "BraveSoftware/Brave-Browser/User Data",
            "chrome": local / "Google/Chrome/User Data",
            "chromium": local / "Chromium/User Data",
            "edge": local / "Microsoft/Edge/User Data",
            "vivaldi": local / "Vivaldi/User Data",
            "opera": roaming / "Opera Software/Opera Stable",
        }
    if sysname == "Darwin":
        app = home / "Library/Application Support"
        return {
            "brave": app / "BraveSoftware/Brave-Browser",
            "chrome": app / "Google/Chrome",
            "chromium": app / "Chromium",
            "edge": app / "Microsoft Edge",
            "vivaldi": app / "Vivaldi",
            "opera": app / "com.operasoftware.Opera",
        }
    config_dir = Path(os.getenv("XDG_CONFIG_HOME", str(home / ".config")))
    return {
        "brave": config_dir / "BraveSoftware/Brave-Browser",
        "chrome": config_dir / "google-chrome",
        "chromium": config_dir / "chromium",
        "edge": config_dir / "microsoft-edge",
        "vivaldi": config_dir / "vivaldi",
        "opera": config_dir / "opera",
    }


def _firefox_root() -> Path:
    home = Path.home()
    sysname = platform.system()
    if sysname == "Windows":
        return Path(os.getenv("APPDATA", str(home / "AppData/Roaming"))) / "Mozilla/Firefox"
    if sysname == "Darwin":
        return home / "Library/Application Support/Firefox"
    return home / ".mozilla/firefox"


def _chromium_profiles(root: Path) -> list:
    """(profile_dir, display_name) pairs for a Chromium root. Reads
    `Local State` → profile.info_cache for the dir→display-name map (so a profile
    the user renamed in the browser shows through), falling back to on-disk Default/Profile*
    dirs, then to ("Default", "Default")."""
    info = {}
    ls = root / "Local State"
    if ls.exists():
        try:
            data = json.loads(ls.read_text(encoding="utf-8", errors="replace"))
            info = (data.get("profile") or {}).get("info_cache") or {}
        except Exception:
            info = {}
    profiles = []
    if info:
        for dirname, meta in info.items():
            profiles.append((dirname, (meta or {}).get("name") or dirname))
    elif root.exists():
        for p in sorted(root.iterdir()):
            if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile")):
                profiles.append((p.name, p.name))
    return profiles or [("Default", "Default")]


# ── Chromium App-Bound Encryption (issue #46) ────────────────────────────────
# Chromium 127+ on Windows binds the cookie-encryption key to the browser
# executable via an elevation service, so no external process can unwrap it —
# `--cookies-from-browser` can never succeed for Brave/Chrome/Edge/Vivaldi on
# such a profile. yt-dlp reports it as a bare "Failed to decrypt with DPAPI",
# which reads like a transient permissions problem; it is permanent. We detect
# it so the UI can say so and route the user to the extension-export path
# ("Load cookies.txt" / "Paste cookies"), which works because an extension runs
# inside the browser. Detection is diagnostic only — never an abort, never a
# workaround attempt, and Windows-only (ABE has no Linux/macOS equivalent).
APP_BOUND_ENCRYPTION = "app_bound_encryption"

# yt-dlp's decrypt-failure wording (yt-dlp issues 10927 / 7271). A locked cookie
# DB ("Could not copy ... cookie database") is a *different*, possibly transient
# failure and must keep its own reason — so match the decrypt wording only.
_DECRYPT_FAILURE_HINTS = ("failed to decrypt", "dpapi")


def _looks_like_decrypt_failure(msg) -> bool:
    """True when an extraction error is a cookie *decryption* failure rather
    than a locked/absent database. Total — never raises."""
    try:
        low = str(msg or "").lower()
    except Exception:
        return False
    return any(h in low for h in _DECRYPT_FAILURE_HINTS)


def _chromium_uses_app_bound_encryption(root: Path) -> bool:
    """True when a Chromium user-data root's `Local State` carries an App-Bound
    Encryption key. Cheap (no DB access, no locked-file copy) and total: a
    missing, empty, malformed or unreadable `Local State` yields False so the
    candidate stays reported as a plain error. Windows-only by definition."""
    if platform.system() != "Windows":
        return False
    try:
        data = json.loads((root / "Local State").read_text(encoding="utf-8", errors="replace"))
        if not isinstance(data, dict):
            return False
        os_crypt = data.get("os_crypt")
        if not isinstance(os_crypt, dict):
            return False
        return bool(os_crypt.get("app_bound_encrypted_key"))
    except Exception:
        return False


def _failure_reason(root: Path, error_msg) -> str:
    """Machine-readable reason for a failed Chromium candidate, or "" when the
    failure has no more specific explanation than its error string."""
    if _looks_like_decrypt_failure(error_msg) and _chromium_uses_app_bound_encryption(root):
        return APP_BOUND_ENCRYPTION
    return ""


class _QuietYDLLogger:
    """Silences yt-dlp's chatty extraction output; we surface our own status."""
    def debug(self, *a, **k): pass
    info = warning = error = trouble = debug


def _extract_one(browser: str, profile):
    """Extract cookies for one (browser, profile) into a YoutubeDLCookieJar.
    Blocking (subprocess + desktop keyring). Raises on any failure."""
    from yt_dlp.cookies import extract_cookies_from_browser
    return extract_cookies_from_browser(browser, profile=profile, logger=_QuietYDLLogger())


def _count_youtube_cookies(jar) -> tuple:
    total = yt = 0
    for c in jar:
        total += 1
        dom = (getattr(c, "domain", "") or "").lower()
        if "youtube" in dom or "google" in dom:
            yt += 1
    return total, yt


def _save_jar(jar, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    jar.save(str(path), ignore_discard=True, ignore_expires=True)


def _scan_all_browser_cookies() -> list:
    """Enumerate installed browsers + profiles, extract each, count YouTube
    cookies. Returns [(entry_dict, jar_or_None)] sorted by youtube_cookies desc.
    Keeps each jar so the caller can save the winner without re-extracting.
    Blocking — call via asyncio.to_thread."""
    results = []
    for browser, root in _browser_roots().items():
        if not root.exists():
            continue
        for profile_dir, display in _chromium_profiles(root):
            entry = {"browser": browser, "profile_dir": profile_dir,
                     "display_name": display, "cookies": 0, "youtube_cookies": 0}
            jar = None
            try:
                jar = _extract_one(browser, profile_dir)
                entry["cookies"], entry["youtube_cookies"] = _count_youtube_cookies(jar)
            except Exception as e:
                entry["error"] = str(e)[:200]
                reason = _failure_reason(root, entry["error"])   # total; cannot raise
                if reason:
                    entry["reason"] = reason
                jar = None
            results.append((entry, jar))
    ff = _firefox_root()
    if ff.exists():
        entry = {"browser": "firefox", "profile_dir": None,
                 "display_name": "default", "cookies": 0, "youtube_cookies": 0}
        jar = None
        try:
            jar = _extract_one("firefox", None)
            entry["cookies"], entry["youtube_cookies"] = _count_youtube_cookies(jar)
        except Exception as e:
            entry["error"] = str(e)[:200]
            jar = None
        results.append((entry, jar))
    results.sort(key=lambda r: r[0].get("youtube_cookies", 0), reverse=True)
    return results
