# Sharpening & Expanding a GPU Video Upscaling Pipeline (RTX 4080, 2025–2026)

> ## ⚑ Read this first — measured 2026-07-24
>
> The reconnaissance below (which options exist, what's fast, what's a dead end) held up
> well and is worth reading. **The graded recommendations at the bottom did not** — they
> were written for a personal hand-run 4080 pipeline, not for FetchForge, which ingests
> arbitrary YouTube content (including HDR) and has a Windows `scale_cuda` fallback.
> Four of them are wrong here. Everything in this box was verified on the actual box:
> **ffmpeg 8.1.2 / libplacebo 7.360.1 / Fedora-Nobara / RTX 4080.**
>
> | Original claim | Verdict |
> |---|---|
> | Replace CPU `cas` with libplacebo's built-in on-GPU `sharpen` (rec. #1) | ❌ **No such option.** Zero options matching `sharp` in `ffmpeg -h filter=libplacebo`. Upstream dropped `pl_render_params.sharpen` in favour of custom shaders; the ffmpeg filter never exposed it. |
> | The CPU `cas` GPU→CPU→GPU roundtrip "costs time and PCIe bandwidth" | ❌ **Not measurable.** 3.60× realtime with `cas`, 3.68× with a GPU shader instead. The pipeline is bound by decode/encode, not by this. |
> | Prefer `ewa_lanczossharp` + sharpening over shaders on 3D game footage (line ~61) | ❌ **Backwards on our content.** FSRCNNX beat `cas` by ~3× on measured high-frequency energy, artifact-free, at no speed cost. |
> | Drop `tonemapping=none`; hard-tag `colorspace/primaries/trc=bt709` | ❌ **Harmful for FetchForge.** Would mislabel HDR/BT.2020 sources as BT.709 and break the HDR passthrough of issue #33. Fine for known-SDR personal captures only. |
> | Blanket `-preset p7 -tune uhq -cq 30` | ⚠ **Conflicts** with the tuned preset ladder and 4K constraints in CLAUDE.md (uhq rejects p2 at 4K; 4K maxrate hard-capped at 20M). Do not apply globally. |
> | `ewa_lanczossharp`, `ewa_lanczos4sharpest` are available kernels | ✅ Both accepted by this build. |
> | `av1_nvenc` available on Ada | ✅ Present. |
> | RTX VSR is not reachable as an ffmpeg `-vf` filter | ✅ Correct, and still true. |
> | Shaders can be silently inert inside ffmpeg's libplacebo (issue #314) | ✅ **The single most useful line in this doc** — and we now know one concrete mechanism: see "Why a shader silently does nothing" below. |
> | Heavy models (x4plus, cunet, swin_unet, SCUNet) and ncnn are too slow | ✅ Not re-measured, but consistent with everything else observed. |
>
> **Outcome:** the doc's Group B (`custom_shader_path`) turned out to be the win, not its
> Tier 1. That became **issue #37** (FSRCNNX-8 neural upscale). Tier 3 (VapourSynth +
> vs-mlrt) is not worth building unless #37 proves insufficient — it's a large stack for
> an unquantified gain, on 4090-extrapolated numbers, when a free option is already good.

## TL;DR (revised after measurement)
- The current `libplacebo=ewa_lanczos + cas=0.5` chain is a reasonable non-ML baseline, but
  `cas=0.5` is close to invisible: **+14–16%** high-frequency energy over the bare scaler.
  The available cheap upgrades are the sharper kernels (`ewa_lanczossharp`) and a stronger
  `cas` (~0.7–0.75 — `cas=1.0` disintegrates, see Group F). There is **no** built-in
  libplacebo `sharpen` to switch to.
- The real win inside the existing chain is an **mpv-format neural shader via
  `custom_shader_path`**: FSRCNNX-8 measured **+40–48%** HF energy — ~3× what `cas` gives —
  for **no measurable time cost** (3.68× vs 3.60× realtime). This is Group B, and it is the
  recommendation that survived. Tracked as issue #37.
- **VapourSynth + vs-mlrt (TensorRT)** remains the theoretical quality ceiling and is
  correctly described below, but it is now a *fallback*, not the plan: large build, TRT
  engines per resolution, frames through system memory, and no measured gain over FSRCNNX
  on this content.
- NVIDIA RTX VSR is genuinely good but is **not** an ffmpeg `-vf` filter; scripted use means
  the RTX Video SDK or a third-party beta CLI wrapper, outside the filter graph.

## Measured baseline (2026-07-24)

Source: `Final_Fantasy_X-2_HD_Remaster … Prologue_h265.mp4` — 1280×720 HEVC, 59.94 fps,
2289.7 s, 137,246 frames, 1.53 Mb/s. Target 1920×1080. Encoder held constant at
`hevc_nvenc -preset p4 -tune uhq -rc vbr -cq 24 -b:v 0 -maxrate 3M`.

Metric: **high-frequency energy** = standard deviation of a Laplacian convolution on the
grayscale frame, compared at identical 1920×1080. Not a perceptual metric — a proxy for
"how much fine detail survived". Measured on three representative frames (battle HUD,
menu text, character over sky).

| Upscale chain | HF vs bare `ewa_lanczos` | Speed (30 s clip) | Full 38-min file |
|---|---|---|---|
| `ewa_lanczos` only | baseline | — | — |
| `ewa_lanczos` + `cas=0.5` *(shipped)* | +14.6 / +15.8 / +14.1 % | 3.60× realtime | 11 min |
| FSRCNNX-8 (`_8-0-4-1`) | +48.1 / +48.4 / +40.5 % | 3.68× realtime | 10 min |
| FSRCNNX-16 (`_16-0-4-1`) | +48.8 / +49.2 / +38.0 % | 3.42× realtime | 11 min |
| FSRCNNX-16 + `cas=0.5` | +68.8 / +72.1 / +55.3 % | — | — |

**FSRCNNX-8 is the pick**: statistically indistinguishable from the 16 variant, 71 KB vs
247 KB, marginally faster. This directly contradicts the doc's line ~55 claim that
`16-0-4-1 > 8-0-4-1` in quality and that FSRCNNX is only realtime "if you've a really
high-end setup" — at 720p→1080p on a 4080 the shader cost is lost in the noise.

### Why a shader silently does nothing

`FSRCNNX_x2_8-0-4-1.glsl` opens with `//!HOOK LUMA`, so it only fires when libplacebo's
input has a luma plane. Feed it RGB and it is a **no-op with no warning and no error** —
hit while benchmarking: RGB PNG input produced byte-identical output to the plain scaler,
while the real yuv420p video path worked. This is very likely the same root cause as the
libplacebo #314 reports of `adaptive-sharpen.glsl` being inert.

It also carries an internal activation guard:
`//!WHEN OUTPUT.w LUMA.w / 1.300 > OUTPUT.h LUMA.h / 1.300 > *` — it only runs above
**1.3× in both axes**. 720p→1080p (1.5×) fires; 720p→900p (1.25×) silently does not.

**Therefore: never accept "the flag is in the argv" as evidence a shader works.** Verify by
diffing output against the no-shader render (md5, or the HF metric above).

## Current Pipeline: Limitations
Your command:
```
-hwaccel cuda -c:v hevc_cuvid -i IN -vf libplacebo=w=-2:h=1080:upscaler=ewa_lanczos:downscaler=ewa_lanczos:format=yuv420p:tonemapping=none,cas=0.5
```
Issues:
1. **`ewa_lanczos` is not the sharpest EWA option.** libplacebo ships `ewa_lanczossharp` (sharpened) and `ewa_lanczos4sharpest` (very sharp, with anti-ringing). The `high_quality` preset auto-selects `ewa_lanczossharp` and enables debanding.
2. **`cas` runs on CPU and forces a GPU→CPU→GPU roundtrip.** `cas` is a software filter; after libplacebo (Vulkan/GPU) frames, ffmpeg must `hwdownload` to system memory, run CAS on CPU, then re-upload for NVENC. That breaks the all-GPU path and costs time and PCIe bandwidth.
   > ❌ **Measured false as a practical concern (2026-07-24).** True in principle, irrelevant
   > in magnitude: 3.60× realtime with `cas` vs 3.68× with an on-GPU shader in its place.
   > Note also that FetchForge's chain uses `-hwaccel cuda` *without*
   > `-hwaccel_output_format cuda`, so frames are already in system memory when libplacebo
   > gets them — there is no all-GPU path to break. Do not spend effort here.
3. **`tonemapping=none` is redundant for SDR→SDR**, and you are not setting explicit colorspace/primaries/trc, risking BT.709 tagging drift.
   > ❌ **Do not apply to FetchForge.** The tool ingests arbitrary YouTube content, including
   > HDR/10-bit (auto-detected from `color_transfer`/`pix_fmt`, passthrough only). Dropping
   > `tonemapping=none` invites libplacebo to tone-map an HDR source, and hard-tagging
   > `bt709` would *mislabel* BT.2020 footage. `tonemapping=none` is load-bearing here, not
   > redundant. Issue #33 tracks verifying HDR passthrough through libplacebo.
4. **No sigmoid/deband/dither control surfaced** — sigmoidal upscaling (reduces ringing, on by default) and dithering are quality behaviors worth setting explicitly.
5. **Sharpening is fixed and mild.** CAS at 0.5 is reasonable but you have no adaptive/edge-aware alternative, and CAS ordering matters (must be after scale).

---

## Option Group A — Better libplacebo (stay ~current speed)

### A.1 Upscaler kernels
From libplacebo docs, upscalers ordered fastest→slowest include `bilinear`, `bicubic`, `lanczos`, `ewa_lanczos` (slow), `ewa_lanczossharp` (slow, sharpened), and `ewa_lanczos4sharpest` (very slow, with anti-ringing). The mpv developers' sharpness/ringing ranking is: `ewa_lanczos4sharpest > lanczos > ewa_lanczossharp > ewa_hanning`. For clean rendered content, `ewa_lanczossharp` is the sweet spot; `ewa_lanczos4sharpest` is sharper but slower and, per mpv devs, the 3-lobe "sharpest" variant "is subpar for upscaling and has nasty aliasing for a polar filter" (the 4-lobe `ewa_lanczos4sharpest` behaves better).

You can also define a custom kernel via `extra_opts`, e.g. the "EWA LanczosSharp" recipe straight from the ffmpeg docs:
```
-vf "libplacebo=w=iw*2:h=ih*2:extra_opts='upscaler=custom\:upscaler_preset=ewa_lanczos\:upscaler_blur=0.9812505644269356'"
```

### A.2 Built-in quality switches
- `sigmoid=1` (default on) — "Enable sigmoidal compression during upscaling. Reduces ringing slightly." Keep it.
- `deband=1` — libplacebo's debander; docs say "Turning this on is highly recommended whenever quality is desired." For clean game footage banding is rare, so this is optional (small cost).
- `dithering=blue` (default, pseudo-blue-noise) — keep on to avoid banding when outputting 8-bit yuv420p; docs recommend always leaving dithering on for sub-16-bit output.
- `preset=high_quality` — "Reset all structs to their high_quality presets… set the upscaler to ewa_lanczossharp, and enable deband=yes. Suitable for use on machines with a discrete GPU."
- Explicitly set `colorspace=bt709:color_primaries=bt709:color_trc=bt709` for SDR correctness.

### A.3 libplacebo built-in sharpening — ❌ DOES NOT EXIST
~~libplacebo exposes a built-in `sharpen` parameter (a lightweight unsharp-like control) that runs on-GPU inside the same Vulkan pass — strongly preferable to the CPU `cas` filter for keeping the pipeline on-GPU.~~

**This was the doc's headline recommendation and it is not real.** Checked 2026-07-24 on
ffmpeg 8.1.2 / libplacebo 7.360.1:

```
$ ffmpeg -h filter=libplacebo | grep -ci sharp
0
```

The only `sharp` strings the filter accepts are *upscaler kernel names*
(`ewa_lanczossharp`, `ewa_lanczos4sharpest`), not a sharpening amount. libplacebo the
library once had `pl_render_params.sharpen`; it was deprecated and removed upstream in
favour of custom shaders, and the ffmpeg filter never exposed it. If you want sharpening
in this filter graph your options are the CPU `cas` filter (cheap, see Group F) or a
custom shader (Group B) — there is no third door.

---

## Option Group B — GLSL / mpv-style shaders inside libplacebo

libplacebo supports mpv `.hook` custom shaders via `custom_shader_path`. Example:
```
libplacebo=w=iw*2:h=ih*2:custom_shader_path=shaders/Anime4K_Upscale_CNN_x2_VL.glsl
```
Passing `w`/`h` makes libplacebo do its own scaling *around* the shader; to let the shader do the doubling, size the output to match the shader's scale factor.

**Critical caveat:** There is a known issue (haasn/libplacebo #314) where some shaders — e.g. `adaptive-sharpen.glsl` — have **negligible effect inside ffmpeg's libplacebo** while working correctly in mpv or Selur's Hybrid. So shader results in ffmpeg may not match mpv. Test each shader's actual effect before relying on it.

Shader options and suitability for **clean game/rendered footage** (most of these are anime/line-art tuned):
- **FSRCNNX** (`FSRCNNX_x2_16-0-4-1`, `_8-0-4-1`): CNN luma doubler. Sharp; `16-0-4-1` > `8-0-4-1` in quality but heavier and, per community guides, only realtime "if you've a really high-end setup." Trained for anime but usable generally. No longer actively maintained.
  > ✅ **Measured, and better than described (2026-07-24).** On 720p→1080p 3D game footage,
  > `_8-0-4-1` and `_16-0-4-1` are statistically indistinguishable (+48.1/48.4/40.5% vs
  > +48.8/49.2/38.0% HF), so **prefer `_8-0-4-1`** — 71 KB vs 247 KB and marginally faster.
  > "Only realtime on a really high-end setup" is wrong at this scale factor: the shader cost
  > was unmeasurable (3.68× vs 3.60× realtime). "Trained for anime but usable generally" is
  > the accurate half — it worked well here with no over-sharpening of textures.
  > Now the basis of issue #37. Chroma is *not* shader-upscaled (luma hook only) — standard,
  > and not a defect to fix.
- **ArtCNN** (`ArtCNN_C4F32` > `C4F16` > `C4F8`): actively developed, comparable to or slightly better than FSRCNNX for HD luma doubling (Artoriuz's evaluation puts ArtCNN at or near the top for luma doubling); anime-oriented. `C4F32` heaviest.
- **Anime4K**: modular anime restore/upscale shaders. Bloc97 also published **Anime4K_3D** shaders specifically for 3D games; per bloc97 in Blinue/Magpie Discussion #183, "Upscaling from 1080p to 4K takes about ~0.9ms (1100 fps) on my Vega64 GPU," with the caveat that "the shaders perform poorly on the UI of games (a bit of ringing)." This is the most relevant Anime4K variant for rendered game content.
- **NNEDI3** (`nnedi3-nns32-win8x4.hook` etc.): neural edge-directed; higher `nns` = better/slower. Older, heavy.
- **CuNNy / RAVU**: RAVU (`ravu-lite-ar-r3`) is a fast RAISR-style prescaler; CuNNy is a newer fast CNN alternative.

**Reality check for game footage:** ~~these shaders are overwhelmingly trained on **anime/line art**, not photographic/3D-rendered frames. For crisp 3D game footage, `ewa_lanczossharp` + good sharpening, or a *general/photographic* ML model (Group D), will usually beat anime shaders, which can over-sharpen lines and mishandle textures.~~ Anime4K_3D is the exception worth testing.

> ❌ **Falsified on our content (2026-07-24) — and this was the costliest error in the doc,
> because it steers you away from the one thing that worked.** On 720p→1080p PS2-era 3D
> game footage, anime-trained FSRCNNX-8 beat `ewa_lanczos + cas=0.5` by roughly 3× on HF
> energy (+40–48% vs +14–16%) with no visible over-sharpening, no ringing, and no texture
> artifacts — including on flat sky, where `cas` at higher strengths falls apart first.
> The training-domain argument sounds right and did not survive measurement.
>
> Generalisation, stated carefully: on **soft, low-detail, low-noise** source (which is what
> a PS2 remaster upload is), a small luma CNN reconstructs edges better than any kernel +
> local-contrast trick, regardless of what it was trained on. Don't extrapolate this to
> noisy or film-grain source without re-measuring.

---

## Option Group C — NVIDIA hardware paths (VSR / Maxine / TensorRT)

### C.1 RTX VSR
RTX Video Super Resolution uses Tensor Cores to upscale and de-artifact video. Per NVIDIA's official RTX Video FAQ, the January 2025 NVIDIA app update means "VSR has been updated to a more efficient AI model, using up to 30% fewer GPU resources at its highest quality setting, allowing more GeForce RTX GPUs to enable it. VSR now also upscales HDR video." It is exposed for browsers/media players and, since mid-2024, as the **RTX Video SDK**. Originally the SDK shipped with DirectX 11/12 and Vulkan; per NVIDIA's RTX Video SDK release notes, **RTX Video SDK 1.1 now adds native CUDA support plus RTX 50-series (Blackwell) support and improved, faster super-resolution models** — so CUDA is no longer merely "coming soon." Crucially, **it is still not an ffmpeg `-vf` filter.**

For a scripted/batch pipeline, a third-party CLI wrapper **RTXVideoProcessor** (DrC0ns0le) uses the RTX Video SDK (NGX runtime `nvngx_vsr.dll`) as a "drop-in FFmpeg replacement," doing 2× VSR + optional TrueHDR with a zero-copy GPU pipeline. Its README reports "~2-5× real-time speed on RTX 4070 (preset-dependent)" and flags that it "is currently in beta stage and provided as-is." A 4080 would be faster. This is the most practical way to get RTX VSR into an automated pipeline. Caveats: third-party, proprietary NVIDIA DLLs, and VSR is tuned to de-artifact compressed video (helpful but you have clean sources, so the artifact-removal benefit is muted).

### C.2 Maxine VFX SDK
NVIDIA Maxine has a SuperResolution/Upscale effect (VideoEffectsApp). Its VSR filter requires recent drivers (Linux 570.190+/580.82+/590.44+, Windows R595+ for TCC), suggests ≥360p input, and offers denoise/deblur modes aimed at low-light/archival footage — explicitly *not* recommended for structured compression artifacts or intentional grain. Usable for offline upscaling but a separate app, not ffmpeg-integrated, and geared toward webcam/streaming enhancement rather than clean game footage.

### C.3 TensorRT
The fast, practical route to TensorRT-accelerated SR is via **vs-mlrt** (Group E) rather than hand-rolling TensorRT engines. vs-mlrt also offers a `trt_rtx` (TensorRT-RTX) backend that "compiles engines faster with comparable performance" on RTX GPUs.

---

## Option Group D — ML upscalers & realistic 4080 throughput

All fps below are FP16 TensorRT at 1920×1080 unless noted. Primary source: the vs-mlrt RTX 4090 wiki (benchmarks by MysteryDove/WolframRhodium, CUDA graphs enabled) and the enhancr project. A 4080 is roughly 25–30% slower than a 4090.

**RTX 4090, TRT FP16, 1080p (vs-mlrt v14.test4), fps by stream count:**
- **real-esrgan Compact (v2/v3, "xsx2")**: 17.06 / 33.41 / 38.26 fps (1 / 2 / 3 streams)
- waifu2x upconv_7: 20.45 / 41.22 / 61.21 fps
- waifu2x upresnet10: 17.91 / 34.53 / 42.33 fps
- waifu2x cunet / cugan: 13.89 / 25.74 / 25.96 fps
- waifu2x swin_unet: 4.62 fps (too slow)
- SCUNet: ~5.1–5.3 fps (too slow)
- DPIR (denoise): ~18–25 fps

**enhancr Compact/animevideov3 (TensorRT), 1080p→2×:** per the enhancr README benchmark table ("RealESRGAN / animevideov3 (TensorRT) 7.64 · 9.10 · 8.49 · 18.66 · 38.67 fps"), the last two columns are **RTX 3090 Ti = 18.66 fps and RTX 4090 = 38.67 fps**.

**Model families:**
- **Real-ESRGAN**: `RealESRGAN_x4plus` (general/photographic, heavy), `RealESRGAN_x4plus_anime_6B` (anime), `realesr-animevideov3` (Compact SRVGGNetCompact, anime video), `realesr-general-x4v3` ("a tiny small model… not too strong deblur and denoise capacity," general-purpose with `-dn` denoise control — the best general light model, and it can be used at ×1/×2/×3). The SRVGGNetCompact "Compact" architecture is the fast family.
- **AnimeJaNai** (the-database, `mpv-upscale-2x_animejanai`): 2× Real-ESRGAN Compact/UltraCompact/SuperUltraCompact ONNX models "designed specifically for doubling the resolution of HD and SD" content, run via TensorRT by default for real-time 1080p→4K. Author's hardware guidance: an RTX 3080 is recommended for **UltraCompact** realtime at 1080p→4K; an RTX 4090 is stated as required for **Compact** at 1080p→4K.
- **Real-CUGAN / waifu2x**: anime-tuned; CUGAN is bilibili's waifu2x-cunet-derived anime SR.

**Compact throughput (targeted finding):** On a 4090, the Compact model at 1080p→4K measures ~17 fps single-stream but 33–41 fps with 2–3 TensorRT streams; enhancr independently shows 38.67 fps. AnimeJaNai's author states a 4090 is "required" for real-time Compact at 1080p→4K, implying a **4080 is marginal (~24–32 fps extrapolated) for the full Compact model in that hardest direction, while UltraCompact/SuperUltraCompact clear real-time comfortably on a 4080.** Because your target is 1080p *output* (far fewer output pixels than 1080p→4K), real throughput on your workload should be meaningfully higher than these 1080p→4K figures — >48 fps is plausible for UltraCompact.

**realesrgan-ncnn-vulkan vs TensorRT:** the ncnn/Vulkan build is portable (runs on AMD/Intel too) but much slower; TensorRT is roughly 50% faster than ncnn for equivalent models (measured on RIFE: 30.71 fps ncnn vs 45.91 fps TRT on an RTX 3050). One user reported the ncnn general model taking "almost 3 hours" for a 10-second 1080p→4K clip. **Avoid ncnn for throughput; use vs-mlrt TensorRT.**

**Anime vs general/photographic:** For clean 3D game footage, prefer **general/photographic** models (`realesr-general-x4v3`, or a Compact model trained on general content) over anime models, which can invent wrong textures (e.g. "adding weird textures to a clear blue sky"). The Compact fps figures above are measured on anime-video models; a general Compact model of the same architecture runs at similar speed.

---

## Option Group E — VapourSynth + vs-mlrt as orchestration layer

**vs-mlrt** (AmusementClub) provides TensorRT (`vstrt`/`trt`, plus `trt_rtx`), ONNX-CUDA (`vsort`), and Vulkan-ncnn (`vsncnn`) runtimes inside VapourSynth, with built-in support for waifu2x, DPIR, RealESRGANv2/v3, Real-CUGAN, RIFE, SCUNet, and ArtCNN. TensorRT is the fastest backend. Multi-stream (`num_streams=2`) roughly doubles throughput at the cost of VRAM — for Compact models this stays well within your 16 GB (Compact uses ~1–2.5 GB per stream at 1080p).

Why VapourSynth over ffmpeg-only:
- TensorRT inference is far faster than any ffmpeg SR filter and unlocks the full model zoo.
- You keep frame-accurate control and can chain restore→upscale→sharpen.
- Pipe VapourSynth output straight into ffmpeg NVENC: `vspipe --container y4m script.vpy - | ffmpeg -i - -c:v hevc_nvenc ...`

Downsides: setup complexity (VapourSynth + vs-mlrt + a per-resolution TensorRT engine build; TRT engines are resolution-specific unless you set `opt_shapes`/`max_shapes`, which is slower), and vspipe→ffmpeg transfers frames through system memory (not a pure on-GPU path), though the NVENC upload is cheap relative to inference time.

---

## Option Group F — Sharpening: beyond `cas=0.5`

- **Ordering — sharpen *after* scale.** ffmpeg filters run sequentially; "Scale before sharpening sharpens the scaled result. Sharpening before scaling loses that work." FidelityFX itself does upscale then RCAS: "you want to sharpen after scaling for better results."
- **CAS strength:** 0.3–0.6 is a sensible range for clean footage; 0.5 is already moderate. CAS is contrast-adaptive, so it produces fewer halos than `unsharp` — "the visual artifacts/fuzz… are not as obvious as the same amount of sharpening with other filters."
  > ✅ **Measured ladder (2026-07-24), and there is a hard ceiling well below 1.0.** Same
  > `ewa_lanczos` upscale, strength swept, HF energy vs `cas` off, two frames (menu / sky):
  >
  > | `cas` | HF gain | Verdict |
  > |---|---|---|
  > | 0.5 | +6.6% / +4.9% | barely above the scaler — near-invisible in motion |
  > | 0.8 | +24.7% / +19.4% | visibly crisper, clean, no halos |
  > | **1.0** | **+444% / +722%** | **disintegrates** — dense chroma speckle across flat sky and stone; that number is noise, not detail |
  >
  > So `0.3–0.6` understates how mild the low end is and gives no warning about the top of
  > the range. **0.7–0.75 is the sweet spot**; never expose 1.0 to a user.
  > Caveat on absolute values: this ladder was rendered from RGB PNG frames, where `cas`
  > operates on a different input domain than in production; the same `cas=0.5` measured
  > +14–16% through the real yuv420p video path. Treat the *ordering* as solid and the
  > production-path numbers in "Measured baseline" as authoritative.
- **libplacebo built-in `sharpen`:** keeps everything on-GPU (avoids the CPU `cas` roundtrip). Prefer this if staying in libplacebo.
- **`unsharp`:** classic; `unsharp=5:5:1.0:5:5:0.0` is a common luma-only starting point but more prone to halos than CAS.
- **`smartblur`:** edge-preserving; better for gentle detail than sharpening.
- **adaptive-sharpen.glsl** (IGV): high-quality in mpv, but per libplacebo #314 it may be nearly inert inside ffmpeg's libplacebo — verify before relying on it.
- **FidelityFX CAS.glsl / CAS-scaled.glsl** (agyild port): `CAS.glsl` for 1:1, `CAS-scaled.glsl` for upscaling; luma-only. Also better in mpv than in ffmpeg libplacebo.

Best practice for you: sharpen after the upscale, keep strength moderate (CAS ~0.4–0.5 equivalent), and prefer on-GPU sharpening (libplacebo built-in) to avoid the CPU CAS roundtrip.

---

## Option Group G — Practical ffmpeg / NVENC pipeline concerns

### G.1 Keep everything on-GPU
> ⚠ **Premise measured to be worth ~0 (2026-07-24).** The system-RAM roundtrip this section
> exists to remove costs nothing detectable in FetchForge's chain (3.60× vs 3.68× realtime),
> because the pipeline is bound by decode/encode and the whole graph runs at ~5× realtime
> for a 38-minute file either way. The interop below is also build-sensitive and unverified
> here. Read this section as background, not as work to do.

Decode on GPU (cuvid/nvdec) → hand frames to Vulkan for libplacebo → hand back to CUDA for NVENC without a system-RAM roundtrip:
```
-init_hw_device vulkan=vk -filter_hw_device vk -hwaccel cuda -hwaccel_output_format cuda -i IN
-vf "hwupload=derive_device=vulkan,libplacebo=...,hwupload=derive_device=cuda"
-c:v hevc_nvenc ...
```
Notes:
- Some ffmpeg/libplacebo builds need `disable_multiplane=1` on the vulkan device (`-init_hw_device vulkan=vk,disable_multiplane=1`) for CUDA interop; libplacebo v6.292.1 was cited as a compatible pairing. This is build-dependent, so expect trial and error.
- The CPU `cas` filter forces `hwdownload`/`hwupload`; dropping it in favor of libplacebo's built-in `sharpen` keeps the graph on-GPU.
- `scale_cuda` can pre-scale on CUDA if desired, but libplacebo does higher-quality scaling; don't double-scale.

### G.2 NVENC high-quality settings
Per NVIDIA and community benchmarking:
- Presets P1 (fastest) → **P7 (best quality)**; NVIDIA states P7 scales near-linearly with the other presets.
  > ⚠ **Do not apply the `p7` / `cq 30` advice in this section globally to FetchForge.** The
  > tool already has a deliberate preset ladder (`calc_encode_params`): hq → p2 for all
  > 60fps, p4 for 30fps; uhq → p3 at 4K60, p2 sub-4K 60fps, p4 at 30fps. Two documented hard
  > constraints sit underneath it — **uhq rejects p2 at 4K**, and **4K maxrate is hard-capped
  > at 20M** (28M throws `InitializeEncoder failed: invalid param (8)`). A blanket `p7` also
  > costs real time at 4K60, which is the case the ladder exists to protect. The `cq 30`
  > figure additionally comes from a *p7 VMAF-95* study; FetchForge's floor is CQ≤24 for
  > sharpen/upscale jobs (issue #29) and is a different target. Re-tuning CQ against uhq is a
  > legitimate experiment, but it is its own issue, not a global find-and-replace.
- **`-tune uhq`** ("ultra-high quality," added in a recent ffmpeg release) is, per Scott Johnson's scottstuff.net study of 1,193 test encodes (Mar 2025), "dramatically better than hevc_nvenc's defaults, which are pretty bad" — but you must retune CQ: "For p7, VMAF 95 happens at -cq:v 26.2 without uhq and -cq:v 33.7 with uhq." All of these are "downright zippy compared to libx265."
- Use `-rc vbr` with `-cq` (constant-quality VBR); `-rc vbr -b:v 0 -cq N` gives true CQ mode (the `-rc vbr` flag is effectively a no-op alongside `-cq` but harmless).
- Recommended HQ HEVC starting point: `-c:v hevc_nvenc -preset p7 -tune uhq -rc vbr -cq 30 -b:v 0 -pix_fmt yuv420p` (with `uhq`, cq ~28–34 targets high quality; without it, ~24–26).
- **AV1 (`av1_nvenc`)**: available on Ada (4080), better efficiency than HEVC at the same quality; same preset/rc/cq framework.
- 10-bit (`p010le` / `yuv420p10le`) can reduce banding at modest cost if you output HEVC Main10 or AV1.

### G.3 Color/format correctness
- ~~SDR→SDR: no tone mapping needed; drop `tonemapping=none` (it's a no-op) but explicitly tag `colorspace=bt709:color_primaries=bt709:color_trc=bt709`.~~
  > ❌ **Wrong for FetchForge — do not apply.** Same correction as §Limitations 3: the tool
  > ingests arbitrary YouTube content including HDR/BT.2020, so `tonemapping=none` is
  > load-bearing (it stops libplacebo tone-mapping an HDR source) and a hard `bt709` tag
  > would actively mislabel HDR footage. Only valid if you *know* every input is SDR.
- libplacebo always operates internally at 4:4:4, so `format=yuv420p` output re-subsamples — fine for delivery, but be aware scaling is happening even for chroma.
- Avoid redundant `format` conversions between filters (each costs a pass).

---

## Recommended Pipelines (tiered, clean game footage on a 4080)

> The original "~2× realtime budget" framing was pessimistic: the production chain measured
> **3.6–3.7× realtime** for 720p→1080p (38-minute file in ~10–11 min), and the neural shader
> did not move that number. Speed is not the binding constraint at this scale factor, so
> tiers should be chosen on quality and complexity, not on fitting a time budget.

> **The two tiers below have been reordered by measurement.** What was "Tier 2" is the
> recommendation; the original Tier 1 is a fallback whose centrepiece (`sharpen=0.4`) does
> not exist. Both original commands are kept for reference but **neither should be
> copy-pasted** — see the annotations.

### Tier 1 (was Tier 2) — Neural shader in libplacebo ✅ RECOMMENDED, MEASURED

This is the chain that was actually validated, expressed against FetchForge's existing
filter-args builder rather than as a hand-run command:

```
-hwaccel cuda -c:v hevc_cuvid -i IN
-vf libplacebo=w=-2:h=1080:upscaler=ewa_lanczos:downscaler=ewa_lanczos\
:custom_shader_path=<PKG_DIR>/shaders/FSRCNNX_x2_8-0-4-1.glsl\
:format=yuv420p:tonemapping=none,cas=0.5
-c:v hevc_nvenc -preset p4 -tune uhq -rc vbr -cq 24 -b:v 0 -maxrate 3M -bufsize 6M
```

- Measured **+40–48%** HF energy vs the bare scaler, at **3.68× realtime** (vs 3.60× for the
  current `cas`-only chain). Effectively free.
- Keeps `tonemapping=none` (needed for HDR sources — see the correction at §Limitations 3)
  and keeps the existing preset/CQ ladder rather than jumping to p7/cq30.
- `cas` composes on top and is worth keeping (FSRCNNX + `cas=0.5` measured +55–72%).
- **Verify the shader fires** — diff against the no-shader render. See "Why a shader
  silently does nothing". Do not trust argv inspection.
- Tracked as **issue #37**.

### Tier 2 (was Tier 1) — Sharper kernel only ⚠ FALLBACK, PARTLY INVALID

```
# ⚠ DO NOT COPY AS-IS: `sharpen=0.4` does not exist (§A.3); the bt709 tags break HDR
#   sources (§Limitations 3); p7/cq30 conflicts with the preset ladder (§G.2).
ffmpeg -init_hw_device vulkan=vk -filter_hw_device vk \
  -hwaccel cuda -hwaccel_output_format cuda -c:v hevc_cuvid -i IN \
  -vf "hwupload=derive_device=vulkan,\
libplacebo=w=-2:h=1080:upscaler=ewa_lanczossharp:downscaler=ewa_lanczos:\
sigmoid=1:deband=0:dithering=blue:sharpen=0.4:\
colorspace=bt709:color_primaries=bt709:color_trc=bt709:\
format=yuv420p,hwupload=derive_device=cuda" \
  -c:v hevc_nvenc -preset p7 -tune uhq -rc vbr -cq 30 -b:v 0 OUT.mp4
```

Salvageable core: swap `upscaler=ewa_lanczos` → `ewa_lanczossharp` (both verified available)
and raise `cas` to ~0.7. Unquantified — the kernel swap was never measured head-to-head
against FSRCNNX, because the test source was deleted before that run. **Open work.**

The `hwupload=derive_device=…` Vulkan↔CUDA interop wrapper is also unverified on this build,
and the doc itself flags it as needing `disable_multiplane=1` trial-and-error. Since the
roundtrip it exists to eliminate was measured to cost nothing (§Limitations 2), this whole
construction is effort with no established payoff. Deprioritise.

### Tier 2b — Other shader families (untested here)
For line-art/2D content, `ArtCNN_C4F16` and Anime4K_3D remain worth testing per Group B;
neither was measured. Same verification rule applies.

### Tier 3 — ML upscaler via VapourSynth + vs-mlrt ⚠ DEMOTED TO FALLBACK
> Correctly described, but no longer the plan. A free `custom_shader_path` shader (Tier 1)
> captured most of the available gain at zero speed cost, so this stack's case is now:
> large build, per-resolution TensorRT engines, frames through system memory, and *zero*
> measured evidence it beats FSRCNNX on this content. Build it only if Tier 1 measurably
> underdelivers on real footage. The "within budget" claim also rests on 4090 extrapolation.
`upscale.vpy`:
```python
import vapoursynth as vs
from vsmlrt import Backend, RealESRGANv2   # or a Compact/general model wrapper
core = vs.core
clip = core.lsmas.LWLibavSource("IN.mkv")
clip = core.resize.Bicubic(clip, format=vs.RGBS, matrix_in_s="709")
clip = core.akarin.Expr(clip, "x 0 1 clamp")   # clamp to avoid TRT overflow artifacts
up = RealESRGANv2(clip, backend=Backend.TRT(fp16=True, num_streams=2))  # Compact/UltraCompact general model
up = core.resize.Bicubic(up, format=vs.YUV420P8, matrix_s="709")
up.set_output()
```
Encode:
```
vspipe --container y4m upscale.vpy - | \
ffmpeg -i - -c:v hevc_nvenc -preset p7 -tune uhq -rc vbr -cq 30 -b:v 0 OUT.mp4
```
Use a **Compact or UltraCompact general/photographic** model. Expected: real ML detail reconstruction beyond any filter/shader. Speed: Compact ~real-time to ~1.3× on a 4080 with 2 streams; UltraCompact well above 2× realtime. Fits the 2× budget; the full x4plus/cunet/swin_unet models do not. First run pays a one-time TensorRT engine-build cost per resolution.

### Optional Tier 4 — RTX VSR via CLI wrapper (hands-off, proprietary)
Use RTXVideoProcessor (RTX Video SDK) for 2× VSR at ~2–5× realtime on a 4070-class GPU (faster on a 4080). Good if you want NVIDIA's tuned model without building a VapourSynth stack; less control, third-party/beta, and its de-artifacting strength is somewhat wasted on already-clean sources.

---

## Recommendations (rewritten 2026-07-24 — the original four are below, struck)

1. **Ship FSRCNNX-8 via `custom_shader_path`** behind an opt-in toggle — issue **#37**.
   Measured ~3× the sharpening benefit of `cas=0.5` for no measurable time cost. Highest
   ROI by a wide margin. Gate the UI control on the ≥1.3× scale-factor guard, bundle the
   shader as package data, and assert in tests that output *differs* from the no-shader
   render (argv assertions are not sufficient).
2. **Raise `CAS_STRENGTH` from 0.5 toward 0.7–0.75** (`server.py`, currently a single local
   constant). Independent of #37 and composes with it. Never expose 1.0.
3. **Measure `ewa_lanczossharp` vs `ewa_lanczos`** head-to-head — one-line change, both
   kernels verified available, gain unknown. Cheapest remaining unknown.
4. **Consider AV1 (`av1_nvenc`)** for delivery — verified present on this box. Unchanged
   from the original doc, still untested.
5. **Do not build the VapourSynth + vs-mlrt stack** unless #37 measurably underdelivers on
   real content. Large build, per-resolution TRT engines, frames through system memory, and
   its case rests on 4090 extrapolations against a free alternative that already works.
6. **Ignore the Vulkan↔CUDA interop rework.** The roundtrip it removes costs nothing here.

<details><summary>Original recommendations (superseded — kept for provenance)</summary>

1. ~~**Immediately** adopt Tier 1: swap to `ewa_lanczossharp`, keep `sigmoid=1`, tag BT.709, and replace CPU `cas=0.5` with libplacebo's on-GPU `sharpen` (~0.4). Add `-preset p7 -tune uhq -rc vbr -cq 30`. Highest ROI, lowest risk.~~ — `sharpen` doesn't exist; BT.709 tagging breaks HDR; p7/cq30 conflicts with the preset ladder.
2. ~~**Evaluate** Tier 3 (vs-mlrt TensorRT, UltraCompact then Compact *general* model) on a representative clip.~~ — demoted to fallback; a free shader got most of the way there.
3. ~~**Test** Tier 2 shaders only for specific 2D/line-art content~~ — inverted: the shader is now the primary recommendation, on 3D content.
4. **Consider** AV1 (`av1_nvenc`) for delivery to cut bitrate at equal quality on your Ada GPU. — still stands.

</details>

### Thresholds that change the recommendation
- If measured throughput for a Compact model on your 4080 drops below ~1× realtime for your target resolution, fall back to UltraCompact/SuperUltraCompact, or to Tier 1.
- If you move to 4K output (not 1080p), the ML tiers will likely fall out of the 2× budget — stay on libplacebo Tier 1/2.
- If sources become compressed/noisy (not clean), add DPIR denoise (vs-mlrt) or RTX VSR's artifact reduction; on clean footage, skip denoising entirely.

## Caveats

**Provenance note.** Everything not marked with a ✅/❌/⚠ callout is **unverified desk
research** — plausible, sourced, and in several cases wrong. The callouts and the "Measured
baseline" section are the only parts backed by measurement on this hardware. When in doubt,
measure; this document's own track record is roughly two-thirds accurate.

**What is still unmeasured** (do not treat as settled):
- `ewa_lanczossharp` vs `ewa_lanczos` gain (rec. #3)
- Anime4K_3D, ArtCNN, ravu, CuNNy on this content
- Any vs-mlrt / TensorRT figure on a 4080
- AV1 vs HEVC at equal quality here
- Whether FSRCNNX holds up on noisy/grainy or film source rather than clean 3D render
- HDR/10-bit behaviour through a luma-hook shader (interacts with issue #33)

- fps figures are RTX 4090 (vs-mlrt/enhancr); **no direct 4080 numbers were found in public benchmarks.** A 4080 is ~25–30% slower, so treat "Compact at 1080p→4K" as marginal and validate empirically.
- Most published numbers are 1080p **input → 4K output**; your workload (output height 1080) produces far fewer pixels, so real throughput on your task should be higher than the quoted figures.
- libplacebo shader behavior in ffmpeg can differ from mpv (issue #314); always verify a shader actually changes output before trusting it.
- The on-GPU CUDA↔Vulkan interop is build-sensitive (`disable_multiplane=1`, libplacebo/ffmpeg version pairing); expect some setup friction with your specific binary.
- RTX VSR/Maxine are proprietary and not first-class ffmpeg filters; the CLI wrapper is community-maintained and in beta.
- `-tune uhq` requires a recent ffmpeg build (roughly ffmpeg 7.x-era `hevc_nvenc`); confirm your binary supports it, and re-tune `-cq` if you toggle it.