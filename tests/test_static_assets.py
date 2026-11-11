"""Issue #107: the frontend CSS and JS are package files, served locally.

Run: .venv/bin/python -m unittest discover -s tests -v

The failure mode this guards is the same one as the fonts: a file that is not
in ``package-data`` works from a source checkout and 404s from a pip install.
"""
import asyncio
import importlib.util
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from fnmatch import fnmatch
from pathlib import Path

from fastapi.responses import FileResponse

from fetchforge import server, config

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
INDEX_HTML = config.PKG_DIR / "index.html"
APP_CSS = config.PKG_DIR / "app.css"
APP_JS = config.PKG_DIR / "app.js"

STATIC_TYPES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}


def _get(name):
    return asyncio.run(server.static_asset(name))


class TestStaticRoute(unittest.TestCase):
    def test_serves_each_allowlisted_file_with_the_right_media_type(self):
        for name, media_type in STATIC_TYPES.items():
            with self.subTest(name=name):
                resp = _get(name)
                self.assertIsInstance(resp, FileResponse)
                self.assertEqual(resp.media_type, media_type)
                self.assertEqual(Path(resp.path), config.PKG_DIR / name)

    def test_sends_no_cache_header(self):
        # Same URL every release, so a long max-age would run old JS/CSS against a
        # new index.html. no-cache revalidates each load (ETag/Last-Modified → 304).
        for name in STATIC_TYPES:
            with self.subTest(name=name):
                resp = _get(name)
                self.assertEqual(resp.headers.get("cache-control", ""), "no-cache")

    def test_rejects_anything_outside_the_allowlist(self):
        for name in (
            "index.html",
            "server.py",
            "config.py",
            "fonts/outfit-latin.woff2",
            "../server.py",
            "..%2Fserver.py",
            "../../pyproject.toml",
            "",
            "app.css/../../server.py",
        ):
            with self.subTest(name=name):
                self.assertEqual(_get(name).status_code, 404)


class TestIndexWiring(unittest.TestCase):
    def test_index_loads_the_split_files_from_the_local_route(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn('<link rel="stylesheet" href="/static/app.css">', html)
        self.assertIn('<script src="/static/app.js"></script>', html)

    def test_token_injection_stays_in_index_and_js_reads_the_meta_tag(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn("__DLPR_TOKEN__", html)
        self.assertNotIn("__DLPR_TOKEN__", APP_JS.read_text(encoding="utf-8"))
        self.assertIn("dlpr-token", APP_JS.read_text(encoding="utf-8"))


class TestPackaging(unittest.TestCase):
    def test_package_data_globs_cover_both_files(self):
        with PYPROJECT.open("rb") as fh:
            cfg = tomllib.load(fh)
        globs = cfg["tool"]["setuptools"]["package-data"]["fetchforge"]
        for name in STATIC_TYPES:
            with self.subTest(name=name):
                self.assertTrue(
                    any(fnmatch(name, g) for g in globs),
                    "%s matches no package-data glob in %r" % (name, globs),
                )

    def test_pip_built_wheel_contains_both_files(self):
        if importlib.util.find_spec("build") is None:
            self.skipTest("the 'build' module is not installed (pip install fetchforge[test])")
        with tempfile.TemporaryDirectory() as out:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--wheel",
                    "--no-isolation",
                    "--outdir",
                    out,
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            wheels = list(Path(out).glob("*.whl"))
            self.assertEqual(len(wheels), 1, "expected exactly one wheel, got %r" % (wheels,))
            with zipfile.ZipFile(wheels[0]) as zf:
                names = set(zf.namelist())
        for name in STATIC_TYPES:
            with self.subTest(name=name):
                self.assertIn("fetchforge/" + name, names)


if __name__ == "__main__":
    unittest.main()
