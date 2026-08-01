"""Issue #53: the two typefaces are served by this process, never by a CDN.

Run: .venv/bin/python -m unittest discover -s tests -v

These guard the three ways self-hosting silently regresses:
  * a `fonts.g*` URL creeping back into index.html,
  * an @font-face src that points at a file that is not shipped,
  * package-data in pyproject.toml not covering the asset, which works fine from
    a source checkout and 404s from a pip install.
"""
import asyncio
import re
import tomllib
import unittest
from fnmatch import fnmatch
from pathlib import Path

from fastapi.responses import FileResponse

from fetchforge import server

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
INDEX_HTML = server.PKG_DIR / "index.html"

WOFF2_MAGIC = b"wOF2"


def _head() -> str:
    """Everything up to </style> — where the font wiring lives."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    return html[: html.index("</style>")]


class TestNoCdnReferences(unittest.TestCase):
    def test_index_html_has_no_google_fonts_host(self):
        html = INDEX_HTML.read_text(encoding="utf-8").lower()
        for host in ("fonts.googleapis.com", "fonts.gstatic.com"):
            self.assertNotIn(host, html, "index.html still reaches out to " + host)

    def test_index_html_has_no_stylesheet_link_at_all(self):
        # A <link rel=stylesheet> can only be remote here: there is no build step
        # and the app serves a single file.
        self.assertNotRegex(
            INDEX_HTML.read_text(encoding="utf-8"),
            r'rel=["\']stylesheet["\']',
        )


class TestFontFaceWiring(unittest.TestCase):
    def setUp(self):
        self.head = _head()
        self.blocks = re.findall(r"@font-face\s*\{(.*?)\}", self.head, re.S)

    def test_both_families_declared(self):
        families = {
            m.group(1)
            for b in self.blocks
            for m in [re.search(r"font-family:\s*'([^']+)'", b)]
            if m
        }
        self.assertEqual(families, {"Outfit", "JetBrains Mono"})

    def test_every_src_is_a_shipped_allowlisted_font(self):
        srcs = re.findall(r"url\('(/fonts/[^']+)'\)", self.head)
        self.assertEqual(len(srcs), 4, "expected 4 @font-face src URLs, got %r" % (srcs,))
        for src in srcs:
            name = src.rsplit("/", 1)[-1]
            with self.subTest(src=src):
                self.assertIn(name, server.FONT_FILES, "not served by /fonts/{name}")
                self.assertTrue((server.FONTS_DIR / name).is_file(), "not on disk")

    def test_every_face_keeps_font_display_swap(self):
        # Dropping this reintroduces FOIT, which is what the CDN's &display=swap
        # was there to prevent.
        for block in self.blocks:
            self.assertRegex(block, r"font-display:\s*swap\s*;")

    def test_woff2_only(self):
        self.assertEqual(re.findall(r"format\('([^']+)'\)", self.head), ["woff2"] * 4)

    def test_jetbrains_mono_weight_range_is_not_widened(self):
        # The shipped file's wght axis runs to 800, but the CDN only ever declared
        # 400 and 500, so mono rules asking for 700 clamp to 500. Widening this
        # range silently makes .card-num / .failure-item-cat heavier.
        mono = [b for b in self.blocks if "'JetBrains Mono'" in b]
        self.assertEqual(len(mono), 2)
        for block in mono:
            self.assertRegex(block, r"font-weight:\s*400 500\s*;")

    def test_outfit_weight_range_covers_the_weights_the_css_uses(self):
        outfit = [b for b in self.blocks if "'Outfit'" in b]
        self.assertEqual(len(outfit), 2)
        for block in outfit:
            self.assertRegex(block, r"font-weight:\s*400 800\s*;")

    def test_preloads_point_at_served_fonts(self):
        preloads = re.findall(r'<link rel="preload"[^>]*href="(/fonts/[^"]+)"[^>]*>', self.head)
        self.assertTrue(preloads, "no font preloads found")
        for href in preloads:
            with self.subTest(href=href):
                # A preload for a URL the page never requests logs a console warning.
                self.assertIn("url('%s')" % href, self.head)
                self.assertIn(href.rsplit("/", 1)[-1], server.FONT_FILES)


class TestShippedAssets(unittest.TestCase):
    def test_font_allowlist_is_non_empty_and_woff2_only(self):
        self.assertTrue(server.FONT_FILES)
        for name in server.FONT_FILES:
            self.assertTrue(name.endswith(".woff2"))

    def test_files_are_real_woff2(self):
        for name in server.FONT_FILES:
            with self.subTest(name=name):
                with (server.FONTS_DIR / name).open("rb") as fh:
                    self.assertEqual(fh.read(4), WOFF2_MAGIC)

    def test_ofl_licence_ships_for_each_family(self):
        # Public repo, OFL fonts: the licence text must travel with the binaries.
        for licence in ("OFL-Outfit.txt", "OFL-JetBrainsMono.txt"):
            path = server.FONTS_DIR / licence
            with self.subTest(licence=licence):
                self.assertTrue(path.is_file(), "%s is missing" % licence)
                text = path.read_text(encoding="utf-8")
                self.assertIn("SIL OPEN FONT LICENSE VERSION 1.1", text.upper())
                self.assertIn("COPYRIGHT", text.upper())

    def test_provenance_note_ships(self):
        self.assertTrue((server.FONTS_DIR / "SOURCES.txt").is_file())


class TestPackaging(unittest.TestCase):
    """The classic silent failure: fine from a checkout, 404 from a wheel."""

    def test_package_data_globs_cover_every_shipped_font_asset(self):
        with PYPROJECT.open("rb") as fh:
            cfg = tomllib.load(fh)
        globs = cfg["tool"]["setuptools"]["package-data"]["fetchforge"]
        for path in sorted(server.FONTS_DIR.iterdir()):
            rel = "fonts/" + path.name
            with self.subTest(rel=rel):
                self.assertTrue(
                    any(fnmatch(rel, g) for g in globs),
                    "%s matches no package-data glob in %r" % (rel, globs),
                )


class TestFontRoute(unittest.TestCase):
    def _get(self, name):
        return asyncio.run(server.font_asset(name))

    def test_serves_each_allowlisted_font(self):
        for name in sorted(server.FONT_FILES):
            with self.subTest(name=name):
                resp = self._get(name)
                self.assertIsInstance(resp, FileResponse)
                self.assertEqual(resp.media_type, "font/woff2")
                self.assertEqual(Path(resp.path), server.FONTS_DIR / name)

    def test_sends_a_cache_header(self):
        resp = self._get(next(iter(server.FONT_FILES)))
        self.assertIn("max-age", resp.headers.get("cache-control", ""))

    def test_rejects_anything_outside_the_allowlist(self):
        # Non-fonts that live in the same directory must not be reachable, and
        # neither must package source. The route is exact-match, so traversal has
        # nowhere to go — assert that rather than trusting it.
        for name in (
            "SOURCES.txt",
            "OFL-Outfit.txt",
            "index.html",
            "server.py",
            "../server.py",
            "..%2Fserver.py",
            "../../pyproject.toml",
            "",
            "outfit-latin.woff2/../../server.py",
        ):
            with self.subTest(name=name):
                resp = self._get(name)
                self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
