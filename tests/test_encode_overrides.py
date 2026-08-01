"""User encode-override validation (#50).

Every value covered here originates in an HTTP form field and ends up in a
subprocess argv, so the tests are deliberately paranoid: each row of the issue's
constraint matrix gets a happy-path test *and* a garbage-input test, and the
whole auto path is pinned against a frozen copy of the pre-#50 implementation.

`_frozen_calc_encode_params` below is a verbatim copy of `calc_encode_params` as
it stood before this change. It is the regression guard: if any refactor
perturbs the auto path for even one point of the input grid, the comparison in
`TestAutoPathUnchanged` fails.
"""
import itertools
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fetchforge import server

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


# ─────────────────────────────────────────────────────────────────────────────
# Frozen pre-#50 implementation — DO NOT "fix" or refactor this. It only has to
# keep behaving exactly like the code did before overrides were validated.
# ─────────────────────────────────────────────────────────────────────────────
def _frozen_calc_encode_params(height, fps, video_bitrate_kbps, codec, pix_fmt,
                               color_transfer, cq_override=0, maxrate_override="",
                               nvenc_tune="uhq", target_res=0, sharpen=False):
    source_height = height
    if target_res and target_res > 0:
        height = target_res

    if cq_override > 0:
        cq = cq_override
    elif height >= 2160:
        cq = 22
    elif height >= 1440:
        cq = 24
    elif height >= 1080:
        cq = 26
    else:
        cq = 28

    if nvenc_tune == "hq" and cq_override == 0:
        cq = max(cq - 2, 16)

    efficiency = {
        "h264": 0.55,
        "vp9": 0.80,
        "av1": 0.92,
        "hevc": 0.88,
    }.get(codec.lower(), 0.70)

    res_ceil = next(
        (v for k, v in [(2160, 20), (1440, 12), (1080, 7), (720, 4)] if height >= k), 4
    )
    if fps > 35 and height < 2160:
        res_ceil = round(res_ceil * 1.40)

    res_min = next(
        (v for k, v in [(2160, 8), (1440, 5), (1080, 3), (720, 2)] if height >= k), 2
    )

    derived_mbps = round(video_bitrate_kbps * efficiency / 1000, 1) if video_bitrate_kbps > 0 else None

    if maxrate_override and maxrate_override not in ("", "0"):
        maxrate_mbps = int(maxrate_override.replace("M", ""))
    elif derived_mbps:
        bloom_cap_mbps = max(round(derived_mbps) + 1, round(derived_mbps * 2))
        maxrate_mbps = min(bloom_cap_mbps, res_ceil)
    else:
        maxrate_mbps = max(res_min, res_ceil // 2)

    if nvenc_tune == "hq" and video_bitrate_kbps > 0 and not (maxrate_override and maxrate_override not in ("", "0")):
        source_cap_mbps = video_bitrate_kbps / 1000
        maxrate_mbps = max(min(maxrate_mbps, source_cap_mbps), res_min)

    hdr_transfers = {"smpte2084", "arib-std-b67", "smpte428", "bt2020-10", "bt2020-12"}
    hdr = color_transfer in hdr_transfers
    ten_bit = "10le" in pix_fmt or "10be" in pix_fmt or hdr
    pix_out = "yuv420p10le" if ten_bit else "yuv420p"

    if nvenc_tune == "hq":
        preset = "p2" if fps > 35 else "p4"
    elif fps > 35:
        preset = "p2" if height < 2160 else "p3"
    else:
        preset = "p4"

    is_upscale = target_res > 0 and target_res > source_height
    if sharpen or is_upscale:
        cq = min(cq, 24)
        maxrate_mbps = max(maxrate_mbps, 3)
        if preset == "p2":
            preset = "p4"

    return {
        "cq": cq,
        "preset": preset,
        "maxrate": f"{maxrate_mbps}M",
        "bufsize": f"{maxrate_mbps * 2}M",
        "pix_fmt": pix_out,
        "profile": "main10" if ten_bit else "main",
        "hdr": hdr,
        "ten_bit": ten_bit,
        "bf": 3,
        "b_ref_mode": "middle",
    }


# Keys the pre-#50 dict carried. Anything else is new surface added by #50.
_FROZEN_KEYS = frozenset(_frozen_calc_encode_params(1080, 30.0, 8000, "vp9", "yuv420p", ""))


def _auto_grid():
    """Every combination worth pinning, with no override set anywhere."""
    for height, fps, kbps, codec, tune, target_res, sharpen, transfer in itertools.product(
        (480, 720, 1080, 1440, 2160),
        (24.0, 30.0, 60.0),
        (0, 900, 8000, 100000),
        ("h264", "vp9", "av1", "hevc", "theora"),
        ("hq", "uhq"),
        (0, 720, 1080, 2160),
        (False, True),
        ("", "smpte2084"),
    ):
        yield dict(height=height, fps=fps, video_bitrate_kbps=kbps, codec=codec,
                   pix_fmt="yuv420p", color_transfer=transfer,
                   nvenc_tune=tune, target_res=target_res, sharpen=sharpen)


def _base(**kw):
    args = dict(height=1080, fps=30.0, video_bitrate_kbps=8000, codec="vp9",
                pix_fmt="yuv420p", color_transfer="")
    args.update(kw)
    return server.calc_encode_params(**args)


def _argv(params, tune="hq"):
    return server.build_video_ffmpeg_args(Path("in.mkv"), Path("out.mp4"), params, tune, "vp9")


class TestAutoPathUnchanged(unittest.TestCase):
    """The regression guard for the whole issue: with no override set,
    calc_encode_params must still produce exactly what it produced before."""

    def test_grid_matches_frozen_reference(self):
        checked = 0
        for kw in _auto_grid():
            ref = _frozen_calc_encode_params(**kw)
            got = server.calc_encode_params(**kw)
            self.assertEqual({k: v for k, v in got.items() if k in _FROZEN_KEYS}, ref, kw)
            checked += 1
        self.assertGreater(checked, 2000, "grid collapsed — the guard would be vacuous")

    def test_grid_is_not_vacuous(self):
        """If every grid point produced the same dict the comparison above would
        prove nothing. Assert the inputs really do fan out."""
        distinct = {tuple(sorted(_frozen_calc_encode_params(**kw).items())) for kw in _auto_grid()}
        self.assertGreater(len(distinct), 100, len(distinct))

    def test_explicit_empty_overrides_are_the_same_as_omitting_them(self):
        for kw in ({}, {"height": 2160, "fps": 60.0, "video_bitrate_kbps": 100000},
                   {"nvenc_tune": "hq", "sharpen": True},
                   {"color_transfer": "smpte2084", "target_res": 2160, "height": 1080}):
            plain = _base(**kw)
            explicit = _base(cq_override=0, maxrate_override="", preset_override="",
                             tune_override="", pix_fmt_override="", **kw)
            self.assertEqual(plain, explicit, kw)

    def test_new_keys_are_the_only_addition(self):
        got = _base()
        self.assertEqual(set(got) - _FROZEN_KEYS, {"tune", "overrides"})

    def test_no_overrides_reports_nothing_applied(self):
        self.assertEqual(_base()["overrides"], {})
        self.assertEqual(_base(nvenc_tune="hq")["tune"], "hq")
        self.assertEqual(_base(nvenc_tune="uhq")["tune"], "uhq")


class TestMaxrateCeiling(unittest.TestCase):
    """Matrix row `maxrate`: ""/"0" = auto, "1M"–"25M", clamped to the
    per-resolution ceiling (4K 20 / 1440p 12 / 1080p 7 / 720p 4, ×1.40 for
    sub-4K 60fps only)."""

    def test_25m_on_4k_is_clamped_to_20m(self):
        p = _base(height=2160, fps=60.0, video_bitrate_kbps=100000, maxrate_override="25M")
        self.assertEqual(p["maxrate"], "20M")
        self.assertEqual(p["bufsize"], "40M")

    def test_25m_on_1080p_is_not_over_clamped_below_its_own_ceiling(self):
        self.assertEqual(_base(height=1080, fps=30.0, maxrate_override="25M")["maxrate"], "7M")
        # 1080p60 gets the ×1.40 headroom: 7 → 10.
        self.assertEqual(_base(height=1080, fps=60.0, maxrate_override="25M")["maxrate"], "10M")
        self.assertEqual(_base(height=1440, fps=30.0, maxrate_override="25M")["maxrate"], "12M")
        self.assertEqual(_base(height=720, fps=30.0, maxrate_override="25M")["maxrate"], "4M")

    def test_in_range_override_below_the_ceiling_is_verbatim(self):
        p = _base(height=2160, video_bitrate_kbps=100000, maxrate_override="9M")
        self.assertEqual(p["maxrate"], "9M")
        self.assertEqual(p["bufsize"], "18M")

    def test_bare_number_and_lowercase_suffix_accepted(self):
        self.assertEqual(_base(height=2160, maxrate_override="9")["maxrate"], "9M")
        self.assertEqual(_base(height=2160, maxrate_override="9m")["maxrate"], "9M")
        self.assertEqual(_base(height=2160, maxrate_override=" 9M ")["maxrate"], "9M")

    def test_override_still_opts_out_of_the_hq_source_cap(self):
        """Preserved semantics: an explicit maxrate bypasses the hq source-bitrate
        cap. 2 Mbps source would otherwise drag the ceiling down to 3M."""
        p = _base(height=1080, fps=30.0, video_bitrate_kbps=2000, codec="h264",
                  nvenc_tune="hq", maxrate_override="7M")
        self.assertEqual(p["maxrate"], "7M")

    def test_target_res_drives_the_ceiling_not_source_height(self):
        # Downscaling 4K → 1080p must clamp against the 1080p ceiling.
        p = _base(height=2160, fps=30.0, target_res=1080, maxrate_override="25M")
        self.assertEqual(p["maxrate"], "7M")

    def test_clamp_is_reported(self):
        rep = _base(height=2160, maxrate_override="25M")["overrides"]["maxrate"]
        self.assertEqual(rep["requested"], "25M")
        self.assertEqual(rep["applied"], "20M")
        self.assertEqual(rep["status"], "clamped")

    def test_unclamped_override_reports_applied(self):
        rep = _base(height=2160, maxrate_override="9M")["overrides"]["maxrate"]
        self.assertEqual((rep["requested"], rep["applied"], rep["status"]), ("9M", "9M", "applied"))


class TestClampOrder(unittest.TestCase):
    """The sharpen/upscale floor raises maxrate and the new ceiling lowers it.
    Applied in the wrong order they disagree; a sharpened 4K job must land
    inside 20M *and* above 3M at the same time."""

    def _mbps(self, params):
        return float(params["maxrate"].rstrip("M"))

    def test_sharpened_4k_with_25m_lands_between_floor_and_ceiling(self):
        p = _base(height=2160, fps=60.0, video_bitrate_kbps=100000,
                  maxrate_override="25M", sharpen=True)
        self.assertEqual(p["maxrate"], "20M")
        self.assertLessEqual(self._mbps(p), 20)
        self.assertGreaterEqual(self._mbps(p), 3)

    def test_upscale_to_4k_with_25m_lands_between_floor_and_ceiling(self):
        p = _base(height=1080, video_bitrate_kbps=100000, target_res=2160,
                  maxrate_override="25M", sharpen=True)
        self.assertEqual(p["maxrate"], "20M")

    def test_sharpened_1m_override_is_raised_to_the_3m_floor(self):
        p = _base(height=2160, fps=30.0, video_bitrate_kbps=100000,
                  maxrate_override="1M", sharpen=True)
        self.assertEqual(p["maxrate"], "3M")
        self.assertGreaterEqual(self._mbps(p), 3)

    def test_floor_never_pushes_any_job_past_its_ceiling(self):
        """Exhaustive: no combination of override + floor + tune may exceed the
        per-resolution ceiling."""
        for height, fps, sharpen, tune, mr in itertools.product(
            (480, 720, 1080, 1440, 2160), (30.0, 60.0), (False, True),
            ("hq", "uhq"), ("", "1M", "4M", "13M", "25M"),
        ):
            for target in (0, 2160):
                p = _base(height=height, fps=fps, video_bitrate_kbps=100000,
                          codec="h264", nvenc_tune=tune, sharpen=sharpen,
                          target_res=target, maxrate_override=mr)
                out_h = target or height
                ceil = server._res_ceiling_mbps(out_h, fps)
                self.assertLessEqual(self._mbps(p), ceil,
                                     (height, fps, sharpen, tune, mr, target))


class TestGarbageFallsBackToAuto(unittest.TestCase):
    """Danger zone: no unparseable, out-of-range, whitespace, unit-suffixed,
    negative, float or None value may reach the argv. Every one of them must
    produce exactly the auto result."""

    GARBAGE_MAXRATE = ("abc", "-5", "-5M", "99M", "0M", "20 M",
                       None, "   ", "", "7.5M", 7.5, "M", "1e3M", "--5", "0x10", "26M",
                       "20M;rm -rf /", [], {}, True)
    GARBAGE_CQ = ("abc", "-5", "99", "15", "35", None, "   ", "22.0", 22.5, -1, 15, 35,
                  "2 2", "22M", True, [], "")
    GARBAGE_PRESET = ("p9", "p0", "4", "p", "preset", None, "   ", "p 4", "p4 slow",
                      "P4;ls", 4, 4.0, [], True)
    GARBAGE_TUNE = ("ll", "ull", "lossless", "xyz", None, "   ", "h q", "hq2", 1, [],
                    "uhq!", True)
    GARBAGE_PIX = ("rgb24", "yuv444p", "yuv420p10be", "p010le", None, "   ",
                   "yuv420p 10le", 10, [], "yuv420p;x", True)

    def _assert_auto(self, field, value, **kw):
        auto = _base(**kw)
        got = _base(**{field: value}, **kw)
        self.assertEqual({k: v for k, v in got.items() if k != "overrides"},
                         {k: v for k, v in auto.items() if k != "overrides"},
                         "{}={!r} did not fall back to auto".format(field, value))
        # Rejections are recorded (empty/None are simply "not requested").
        if isinstance(value, str) and value.strip():
            self.assertEqual(got["overrides"].get(field.replace("_override", ""), {}).get("status"),
                             "rejected", "{}={!r} not reported as rejected".format(field, value))

    def test_garbage_maxrate(self):
        for v in self.GARBAGE_MAXRATE:
            with self.subTest(v=v):
                self._assert_auto("maxrate_override", v, height=2160, video_bitrate_kbps=100000)

    def test_garbage_cq(self):
        for v in self.GARBAGE_CQ:
            with self.subTest(v=v):
                self._assert_auto("cq_override", v, height=1080)

    def test_garbage_preset(self):
        for v in self.GARBAGE_PRESET:
            with self.subTest(v=v):
                self._assert_auto("preset_override", v, height=1080, fps=60.0)

    def test_garbage_tune(self):
        for v in self.GARBAGE_TUNE:
            with self.subTest(v=v):
                self._assert_auto("tune_override", v, height=1080, nvenc_tune="uhq")

    def test_garbage_pix_fmt(self):
        for v in self.GARBAGE_PIX:
            with self.subTest(v=v):
                self._assert_auto("pix_fmt_override", v, height=1080)

    def test_garbage_never_reaches_the_argv(self):
        auto = _argv(_base(height=2160, fps=60.0, video_bitrate_kbps=100000), "hq")
        for field, values in (("maxrate_override", self.GARBAGE_MAXRATE),
                              ("cq_override", self.GARBAGE_CQ),
                              ("preset_override", self.GARBAGE_PRESET),
                              ("tune_override", self.GARBAGE_TUNE),
                              ("pix_fmt_override", self.GARBAGE_PIX)):
            for v in values:
                with self.subTest(field=field, v=v):
                    p = _base(height=2160, fps=60.0, video_bitrate_kbps=100000, **{field: v})
                    self.assertEqual(_argv(p, p["tune"] if field == "tune_override" else "hq"), auto)

    def test_surrounding_whitespace_is_tolerated_but_never_forwarded(self):
        """A form field can arrive newline- or tab-padded. Stripping is safe
        because the value is re-emitted as a rebuilt token, never passed through:
        assert the argv gets the clean form, not the padded one."""
        for padded in ("20M\n", "\t20M", " 20M ", "20M\r\n"):
            with self.subTest(v=padded):
                p = _base(height=2160, fps=60.0, video_bitrate_kbps=100000,
                          maxrate_override=padded)
                self.assertEqual(p["maxrate"], "20M")
                argv = _argv(p, p["tune"])
                self.assertEqual(argv[argv.index("-maxrate") + 1], "20M")
        for padded in (" p4 ", "p4\n"):
            with self.subTest(v=padded):
                self.assertEqual(_base(preset_override=padded)["preset"], "p4")
        for padded in (" uhq ", "uhq\n"):
            with self.subTest(v=padded):
                self.assertEqual(_base(tune_override=padded)["tune"], "uhq")

    def test_boundary_values_are_accepted_not_rejected(self):
        """The garbage list brackets the real limits — make sure the limits
        themselves still work, or the fallbacks above would be vacuous."""
        self.assertEqual(_base(height=1080, cq_override=16)["cq"], 16)
        self.assertEqual(_base(height=1080, cq_override=34)["cq"], 34)
        self.assertEqual(_base(height=1080, cq_override="19")["cq"], 19)
        self.assertEqual(_base(height=2160, maxrate_override="1M")["maxrate"], "1M")
        self.assertEqual(_base(height=2160, maxrate_override="25M")["maxrate"], "20M")
        self.assertEqual(_base(height=1080, preset_override="p1")["preset"], "p1")
        self.assertEqual(_base(height=1080, preset_override="p7")["preset"], "p7")
        self.assertEqual(_base(height=1080, preset_override="P4")["preset"], "p4")
        self.assertEqual(_base(height=1080, tune_override="HQ")["tune"], "hq")


class TestTuneOverride(unittest.TestCase):
    """Matrix row `tune`: "" = auto, hq, uhq. uhq must receive no -bf,
    -b_ref_mode, -spatial_aq or -temporal_aq, and must degrade to hq when the
    startup probe says uhq is unavailable."""

    UHQ_FORBIDDEN = ("-bf", "-b_ref_mode", "-spatial_aq", "-temporal_aq", "-aq-strength")

    def test_uhq_override_emits_no_bframe_or_aq_flags(self):
        p = _base(height=2160, fps=60.0, nvenc_tune="hq", tune_override="uhq")
        self.assertEqual(p["tune"], "uhq")
        argv = _argv(p, p["tune"])
        for flag in self.UHQ_FORBIDDEN:
            self.assertNotIn(flag, argv, flag)
        self.assertEqual(argv[argv.index("-tune") + 1], "uhq")

    def test_auto_selected_uhq_also_emits_no_bframe_or_aq_flags(self):
        p = _base(height=2160, fps=60.0, nvenc_tune="uhq")
        argv = _argv(p, p["tune"])
        for flag in self.UHQ_FORBIDDEN:
            self.assertNotIn(flag, argv, flag)

    def test_uhq_override_downgrades_when_probe_says_unsupported(self):
        p = _base(height=2160, fps=60.0, nvenc_tune="hq",
                  tune_override="uhq", probe_tune="hq")
        self.assertEqual(p["tune"], "hq")
        rep = p["overrides"]["tune"]
        self.assertEqual((rep["requested"], rep["applied"], rep["status"]),
                         ("uhq", "hq", "downgraded"))
        argv = _argv(p, p["tune"])
        self.assertEqual(argv[argv.index("-tune") + 1], "hq")
        self.assertIn("-bf", argv)          # hq gets its B-frames back

    def test_uhq_override_survives_a_supported_probe(self):
        p = _base(height=1080, tune_override="uhq", probe_tune="uhq")
        self.assertEqual(p["tune"], "uhq")

    def test_hq_override_applies_the_hq_cq_boost_and_source_cap(self):
        p = _base(height=1080, fps=30.0, video_bitrate_kbps=4000, codec="h264",
                  nvenc_tune="uhq", tune_override="hq")
        self.assertEqual(p["tune"], "hq")
        self.assertEqual(p["cq"], 24)                 # 26 − 2
        self.assertEqual(p["maxrate"], "4M")          # source-bitrate cap (4000 kbps)
        self.assertEqual(p["overrides"]["tune"]["status"], "applied")

    def test_latency_and_lossless_tunes_are_refused(self):
        for bad in ("ll", "ull", "lossless"):
            p = _base(height=1080, nvenc_tune="uhq", tune_override=bad)
            self.assertEqual(p["tune"], "uhq")
            self.assertNotIn(bad, _argv(p, p["tune"]))

    def test_params_tune_drives_the_argv_without_the_caller_re_deriving_it(self):
        """build_video_ffmpeg_args prefers params["tune"] so an override can't be
        lost between calc_encode_params and the argv."""
        p = _base(height=1080, nvenc_tune="hq", tune_override="uhq")
        argv = server.build_video_ffmpeg_args(Path("i.mkv"), Path("o.mp4"), p, "hq", "vp9")
        self.assertEqual(argv[argv.index("-tune") + 1], "uhq")

    def test_legacy_params_without_tune_still_honour_the_argument(self):
        legacy = {"cq": 24, "preset": "p2", "maxrate": "7M", "bufsize": "14M",
                  "pix_fmt": "yuv420p", "profile": "main", "bf": 3, "b_ref_mode": "middle"}
        argv = server.build_video_ffmpeg_args(Path("i.mkv"), Path("o.mp4"), legacy, "hq", "vp9")
        self.assertEqual(argv[argv.index("-tune") + 1], "hq")


class TestPresetOverride(unittest.TestCase):
    """Matrix row `preset`: "" = auto, p1–p7; uhq + p2 is never emitted."""

    def test_valid_preset_is_applied(self):
        p = _base(height=1080, preset_override="p6")
        self.assertEqual(p["preset"], "p6")
        self.assertEqual(_argv(p)[_argv(p).index("-preset") + 1], "p6")
        self.assertEqual(p["overrides"]["preset"]["status"], "applied")

    def test_p2_with_uhq_at_4k_is_not_emitted(self):
        p = _base(height=2160, fps=60.0, nvenc_tune="uhq", preset_override="p2")
        self.assertNotEqual(p["preset"], "p2")
        self.assertEqual(p["preset"], "p4")
        argv = _argv(p, p["tune"])
        self.assertNotEqual(argv[argv.index("-preset") + 1], "p2")
        self.assertEqual(p["overrides"]["preset"]["status"], "clamped")

    def test_p2_with_uhq_arriving_via_tune_override_is_also_bumped(self):
        p = _base(height=2160, fps=60.0, nvenc_tune="hq",
                  preset_override="p2", tune_override="uhq")
        self.assertEqual(p["preset"], "p4")

    def test_p2_with_hq_is_left_alone(self):
        p = _base(height=1080, fps=60.0, nvenc_tune="hq", preset_override="p2")
        self.assertEqual(p["preset"], "p2")
        self.assertEqual(_argv(p, "hq")[_argv(p, "hq").index("-preset") + 1], "p2")

    def test_sharpen_floor_still_bumps_an_explicit_p2(self):
        p = _base(height=1080, fps=60.0, nvenc_tune="hq", preset_override="p2", sharpen=True)
        self.assertEqual(p["preset"], "p4")


class TestPixFmtOverride(unittest.TestCase):
    """Matrix row `pix_fmt`: "" = auto, yuv420p, yuv420p10le. pix_fmt and
    profile always agree."""

    def test_force_10bit_on_sdr_source(self):
        p = _base(pix_fmt="yuv420p", color_transfer="", pix_fmt_override="yuv420p10le")
        self.assertEqual(p["pix_fmt"], "yuv420p10le")
        self.assertEqual(p["profile"], "main10")
        self.assertTrue(p["ten_bit"])
        argv = _argv(p)
        self.assertEqual(argv[argv.index("-profile:v") + 1], "main10")

    def test_force_8bit_on_hdr_source_is_permitted_and_reported(self):
        p = _base(pix_fmt="yuv420p10le", color_transfer="smpte2084",
                  pix_fmt_override="yuv420p")
        self.assertEqual(p["pix_fmt"], "yuv420p")
        self.assertEqual(p["profile"], "main")
        self.assertFalse(p["ten_bit"])
        self.assertTrue(p["hdr"], "the source is still HDR — that fact is not erased")
        rep = p["overrides"]["pix_fmt"]
        self.assertEqual(rep["status"], "applied")
        self.assertEqual(rep["warning"], "hdr_source_forced_8bit")

    def test_pix_fmt_and_profile_never_disagree(self):
        for override in ("", "yuv420p", "yuv420p10le"):
            for transfer in ("", "smpte2084"):
                for src_pix in ("yuv420p", "yuv420p10le"):
                    p = _base(pix_fmt=src_pix, color_transfer=transfer,
                              pix_fmt_override=override)
                    expected = "main10" if p["pix_fmt"] == "yuv420p10le" else "main"
                    self.assertEqual(p["profile"], expected, (override, transfer, src_pix))
                    argv = _argv(p)
                    self.assertEqual(argv[argv.index("-profile:v") + 1], p["profile"])

    def test_no_8bit_warning_when_the_source_is_sdr(self):
        p = _base(pix_fmt="yuv420p", color_transfer="", pix_fmt_override="yuv420p")
        self.assertNotIn("warning", p["overrides"]["pix_fmt"])


class TestArgvInvariants(unittest.TestCase):
    def test_b_v_is_always_zero(self):
        """A non-zero -b:v combined with -cq is rejected by NVENC."""
        for cq in ("", 0, 16, 22, 34, "abc", "99"):
            for mr in ("", "1M", "25M", "abc"):
                p = _base(height=2160, fps=60.0, video_bitrate_kbps=100000,
                          cq_override=cq, maxrate_override=mr)
                argv = _argv(p, p["tune"])
                self.assertEqual(argv[argv.index("-b:v") + 1], "0", (cq, mr))

    def test_maxrate_and_bufsize_in_the_argv_are_the_clamped_values(self):
        p = _base(height=2160, fps=60.0, video_bitrate_kbps=100000, maxrate_override="25M")
        argv = _argv(p, p["tune"])
        self.assertEqual(argv[argv.index("-maxrate") + 1], "20M")
        self.assertEqual(argv[argv.index("-bufsize") + 1], "40M")

    def test_every_argv_token_is_a_clean_string(self):
        """Nothing user-supplied may smuggle whitespace or a shell metacharacter
        into an argv token."""
        for mr, cq, pre, tune, pix in itertools.product(
            ("", "25M", "20 M", "abc"), (0, 22, "-5"), ("", "p4", "p 4"),
            ("", "uhq", "ll"), ("", "yuv420p", "yuv420p;x"),
        ):
            p = _base(height=2160, fps=60.0, video_bitrate_kbps=100000,
                      cq_override=cq, maxrate_override=mr, preset_override=pre,
                      tune_override=tune, pix_fmt_override=pix)
            for tok in _argv(p, p["tune"]):
                self.assertIsInstance(tok, str)
                self.assertNotIn(";", tok)
            self.assertRegex(p["maxrate"], r"^\d+(\.\d+)?M$")
            self.assertRegex(p["preset"], r"^p[1-7]$")
            self.assertIn(p["pix_fmt"], ("yuv420p", "yuv420p10le"))
            self.assertIn(p["tune"], ("hq", "uhq"))


class TestValidateEncodeOverrides(unittest.TestCase):
    """The helper on its own — pure, no globals, no I/O."""

    def _v(self, **kw):
        args = dict(height=1080, fps=30.0)
        args.update(kw)
        return server.validate_encode_overrides(**args)

    def test_defaults_are_all_auto(self):
        v = self._v()
        self.assertEqual(v["cq"], 0)
        self.assertEqual(v["maxrate_mbps"], 0)
        self.assertEqual(v["preset"], "")
        self.assertEqual(v["pix_fmt"], "")
        self.assertEqual(v["tune"], "uhq")     # falls through to auto_tune
        self.assertEqual(v["report"], {})

    def test_auto_tune_is_the_fallback(self):
        self.assertEqual(self._v(auto_tune="hq")["tune"], "hq")
        self.assertEqual(self._v(auto_tune="hq", tune_override="uhq")["tune"], "uhq")

    def test_probe_result_is_read_not_reprobed(self):
        self.assertEqual(self._v(tune_override="uhq", probe_tune="hq")["tune"], "hq")
        self.assertEqual(self._v(auto_tune="uhq", probe_tune="hq")["tune"], "uhq",
                         "auto tune is the caller's business; only overrides are downgraded")

    def test_res_ceiling_table(self):
        self.assertEqual(server._res_ceiling_mbps(2160, 30.0), 20)
        self.assertEqual(server._res_ceiling_mbps(2160, 60.0), 20)   # never bumped at 4K
        self.assertEqual(server._res_ceiling_mbps(1440, 30.0), 12)
        self.assertEqual(server._res_ceiling_mbps(1440, 60.0), 17)
        self.assertEqual(server._res_ceiling_mbps(1080, 30.0), 7)
        self.assertEqual(server._res_ceiling_mbps(1080, 60.0), 10)
        self.assertEqual(server._res_ceiling_mbps(720, 30.0), 4)
        self.assertEqual(server._res_ceiling_mbps(480, 30.0), 4)

    def test_helper_never_raises_on_any_input(self):
        for v in (None, "", "  ", "abc", -1, 999, 1.5, True, [], {}, object()):
            self._v(cq_override=v, maxrate_override=v, preset_override=v,
                    tune_override=v, pix_fmt_override=v)


class TestEndpointFormFields(unittest.TestCase):
    """/download and /convert-local must accept the three new fields, each
    defaulting to "" so clients that never send them behave exactly as before."""

    def test_new_fields_exist_and_default_to_empty(self):
        import inspect
        for endpoint in (server.download, server.convert_local):
            params = inspect.signature(endpoint).parameters
            for name in ("preset", "tune", "pix_fmt"):
                with self.subTest(endpoint=endpoint.__name__, field=name):
                    self.assertIn(name, params)
                    self.assertEqual(params[name].default.default, "")

    def test_pre_existing_fields_keep_their_defaults(self):
        import inspect
        expected = {"cq": "0", "maxrate": "", "tune_mode": "uhq",
                    "target_res": "0", "sharpen": "false"}
        for endpoint in (server.download, server.convert_local):
            params = inspect.signature(endpoint).parameters
            for name, default in expected.items():
                with self.subTest(endpoint=endpoint.__name__, field=name):
                    self.assertEqual(params[name].default.default, default)


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg not on PATH")
class TestValidatedParamsAgainstRealFfmpeg(unittest.IsolatedAsyncioTestCase):
    """CPU ffmpeg, no NVENC: the validated numbers must be accepted by a real
    encoder, and a bad argv must surface a non-zero return code rather than
    being swallowed."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ff50_"))
        cls.clip = cls.tmp / "clip.mkv"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=d=5:s=320x240",
             "-c:v", "libx264", str(cls.clip)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        server._cancel_requested.clear()

    def _cpu_args(self, params, out):
        """The NVENC argv's rate-control shape, on libx264 so it runs anywhere."""
        return ["ffmpeg", "-y", "-loglevel", "info", "-i", str(self.clip),
                "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", params["pix_fmt"],
                "-crf", str(params["cq"]),
                "-b:v", "0",
                "-maxrate", params["maxrate"], "-bufsize", params["bufsize"],
                "-progress", "pipe:1", "-nostats", str(out)]

    async def _run(self, args):
        res: dict = {}
        async for _ in server.run_encode(args, duration_secs=5.0, idx=1, total=1, result=res):
            pass
        return res

    async def test_clamped_4k_override_produces_an_argv_ffmpeg_accepts(self):
        params = _base(height=2160, fps=60.0, video_bitrate_kbps=100000,
                       maxrate_override="25M", sharpen=True)
        self.assertEqual(params["maxrate"], "20M")
        res = await self._run(self._cpu_args(params, self.tmp / "clamped.mp4"))
        self.assertEqual(res["returncode"], 0)
        self.assertTrue((self.tmp / "clamped.mp4").stat().st_size > 0)

    async def test_unvalidated_garbage_would_fail_and_the_failure_is_surfaced(self):
        """This is the pre-#50 failure mode: a bad rate string reaches ffmpeg and
        the job dies at encoder init. run_encode must report it, not swallow it."""
        bad = ["ffmpeg", "-y", "-loglevel", "info", "-i", str(self.clip),
               "-c:v", "libx264", "-maxrate", "abc", "-bufsize", "14M",
               "-progress", "pipe:1", "-nostats", str(self.tmp / "bad.mp4")]
        res = await self._run(bad)
        self.assertNotEqual(res["returncode"], 0)
        self.assertIsNone(server.current_process)


if __name__ == "__main__":
    unittest.main()
