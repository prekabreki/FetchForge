---
description: mpv .hook shaders in libplacebo (custom_shader_path) fail SILENTLY -- LUMA hooks no-op on RGB input and FSRCNNX self-disables below 1.3x scale -- so argv assertions can "pass" while the shader does nothing; also there is NO built-in libplacebo sharpen option
type: project
---

Relevant to issue #37 (FSRCNNX-8 neural upscale). Two traps, both silent.

**1. A `//!HOOK LUMA` shader is a no-op on RGB input — no warning, no error.**
`FSRCNNX_x2_8-0-4-1.glsl` (and most mpv upscale shaders) hook the luma plane, so libplacebo
only runs them when its input actually has one. Feeding PNG/RGB frames produces output
**byte-identical** to the plain scaler while ffmpeg exits 0. Hit while benchmarking: RGB PNG
input md5-matched the no-shader render; the real `yuv420p` video path worked. FetchForge's
production chain decodes to `yuv420p`, so it is fine — but any *test* or *benchmark* that
feeds still images is measuring nothing. This is very likely the same root cause as the
libplacebo #314 reports of `adaptive-sharpen.glsl` being inert inside ffmpeg.

**2. FSRCNNX self-disables below a 1.3x scale factor.**
Its header carries `//!WHEN OUTPUT.w LUMA.w / 1.300 > OUTPUT.h LUMA.h / 1.300 > *` — it only
runs above **1.3x in both axes**. 720p→1080p (1.5x) fires; 1080p→1440p (1.33x) barely does;
720p→900p (1.25x) silently does not. Any UI exposing a target resolution must gate on the
source/target ratio rather than letting the user pick a no-op.

**Therefore: never accept "`custom_shader_path` is in the argv" as evidence the shader ran.**
Prove it by diffing against the no-shader render — md5, or the high-frequency-energy metric
in `docs/research/upscale_research.md`. An argv unit test is necessary but not sufficient;
this is the same lesson as [[scaler-filter-frame-domain]], where a unit test happily asserted
a chain that crashed ffmpeg.

**Bonus, since it wastes time otherwise: libplacebo has no built-in `sharpen`.**
`ffmpeg -h filter=libplacebo | grep -ci sharp` → `0` on ffmpeg 8.1.2 / libplacebo 7.360.1.
The library's `pl_render_params.sharpen` was deprecated and removed upstream in favour of
custom shaders, and the ffmpeg filter never exposed it. The only `sharp` strings the filter
accepts are upscaler *kernel names* (`ewa_lanczossharp`, `ewa_lanczos4sharpest`). So
sharpening in this chain is the CPU `cas` filter or a custom shader — there is no third door,
regardless of what any research doc claims. See `docs/research/upscale_research.md` §A.3.
