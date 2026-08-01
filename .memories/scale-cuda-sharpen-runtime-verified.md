---
description: scale_cuda + cas sharpen branch of _decode_filter_args verified end-to-end on real Windows NVENC hardware (issue #34) -- upscale, upscale+sharpen, and same-size passthrough+sharpen all produce correct-resolution, correct-frame-count, cleanly-decoding output, and cas measurably alters pixels
type: project
---

Issue #34 asked for a REAL runtime check of the `scale_cuda` branch in `_decode_filter_args`
(`fetchforge/server.py`) -- it was argv-tested only because the Linux dev box has no
`scale_cuda`. Ran on Windows / RTX 4080 with `ffmpeg -filters` showing both `scale_cuda` and
`libplacebo` present, monkeypatching `server.HAS_LIBPLACEBO = False` / `HAS_SCALE_CUDA = True`
in-process (no product code touched) to force the branch, then driving real `hevc_nvenc`
encodes through the app's own `build_video_ffmpeg_args` + `run_encode`.

**Source:** synthetic `testsrc2`, 1280x720, 30fps, 150 frames, libx264 yuv420p MP4 (5s).

**Filter chains that ran (real argv, via `_decode_filter_args`):**
- upscale-only (720p->1440p): `scale_cuda=format=yuv420p:w=-2:h=1440`
- upscale+sharpen: `scale_cuda=format=yuv420p:w=-2:h=1440,hwdownload,format=yuv420p,cas=0.72`
- same-size+sharpen (#35 passthrough route, target==source height so
  `_effective_target_height` zeroes it): `scale_cuda=format=yuv420p,hwdownload,format=yuv420p,cas=0.72`
- same-size no-sharpen baseline: `scale_cuda=format=yuv420p`

**Result: all 4 encodes exit 0 AND are verified correct, not just non-crashing:**
| run | resolution | frames | decode errors |
|---|---|---|---|
| upscale_only | 2560x1440 (correct 2x) | 150/150 | 0 |
| upscale_sharpen | 2560x1440 | 150/150 | 0 |
| samesize_sharpen | 1280x720 (correct passthrough) | 150/150 | 0 |
| samesize_nosharpen | 1280x720 | 150/150 | 0 |

**`cas` is genuinely doing work, not silently dropped** (the trap in
[[libplacebo-custom-shader-silent-noop]]): extracted a single lossless PNG frame through the
exact same-size `scale_cuda` filter chain with and without `,cas=0.72` appended (isolating the
filter from NVENC quantization noise). Frames are NOT byte-identical (156387 vs 178213 bytes,
differ from byte 67). PSNR 36.5 dB / SSIM 0.985 between them -- a real, moderate difference.
A `blend=difference` diff shows YAVG=0.38 but YMAX=38 (out of 255): near-zero in flat regions,
strong at edges -- exactly the signature of Contrast Adaptive Sharpen (acts on local contrast,
leaves flat areas alone), not random noise or a no-op.

**One test-harness snag, not a product bug:** the bundled dev ffmpeg here (gyan.dev
2023-10-29 build) predates ffmpeg 7.1 and doesn't know the `uhq` NVENC tune at all
(`_classify_tune_probe` correctly classifies this as `ffmpeg_too_old`). Calling
`calc_encode_params(nvenc_tune="uhq", probe_tune=...)` directly does NOT downgrade an
*auto* tune -- `validate_encode_overrides` only downgrades an *explicit* `tune_override`
(see its docstring: "an auto tune is the caller's business and already reflects the probe").
Production call sites resolve `effective_tune = "hq" if item_tune == "hq" else _nvenc_tune`
themselves *before* calling `calc_encode_params`, using the real startup
`_probe_nvenc_tune()` result. A test driver that skips this step and hardcodes
`nvenc_tune="uhq"` will get `-tune uhq` rejected by ffmpeg with "Unable to parse option
value" / `InitializeEncoder failed` -- that failure is the harness bypassing the tune
resolution step, not the scale_cuda path being broken. Always call
`await server._probe_nvenc_tune()` for real (or otherwise mirror the real call-site
tune resolution) before exercising `calc_encode_params`/`build_video_ffmpeg_args` in a
standalone script.

**Verdict: the scale_cuda + sharpen path works correctly at runtime on this Windows NVENC
build** -- upscale, upscale+sharpen, and same-size+sharpen (passthrough) all produce
dimensionally-correct, frame-count-correct, cleanly-decoding HEVC output, and the CAS sharpen
filter measurably changes the picture rather than being silently dropped.

See [[scaler-filter-frame-domain]] (the hwdownload frame-domain rule this branch depends on)
and [[libplacebo-custom-shader-silent-noop]] (why "argv contains the filter" is not proof it ran).
