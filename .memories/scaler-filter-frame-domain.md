---
description: When appending a CPU filter (cas sharpen) after a GPU scaler in _decode_filter_args, libplacebo needs NO hwdownload (it outputs software frames) but scale_cuda DOES (it outputs CUDA frames) -- mixing them up crashes ffmpeg with -38
type: project
---

The scaling/sharpen `-vf` chain in `_decode_filter_args` (server.py) picks a scaler engine
libplacebo → scale_cuda → CPU lanczos, then optionally appends `cas` (Contrast Adaptive
Sharpen, a CPU filter) for sharpening. The trap is the frame memory-domain the CPU `cas`
receives:

- **libplacebo** here carries `format={pix_fmt}`, so it **outputs software (system-memory)
  frames**. `cas` can follow directly: `libplacebo=...:format=yuv420p:tonemapping=none,cas={CAS_STRENGTH}`.
  Inserting `hwdownload` before `cas` on this branch crashes ffmpeg:
  `Error reinitializing filters ... -38 (Function not implemented)` → encoder never opens.
  (This was the #19→#28 bug: #19 wrongly added `hwdownload,format,cas` to the libplacebo
  branch; #28 dropped it. Caught only by a REAL NVENC encode on the 4080 — the argv unit
  test had actually asserted the broken form.)
- **scale_cuda** outputs **CUDA frames**, so its sharpen branch correctly KEEPS
  `scale_cuda=...,hwdownload,format={pix_fmt},cas={CAS_STRENGTH}` — the download is required there.
  (Windows-only path. **Verified at runtime on Windows/RTX 4080 on 2026-08-01**, #34 —
  see [[scale-cuda-sharpen-runtime-verified]].)
- **CPU scale / passthrough** frames are already in system memory → plain `,cas={CAS_STRENGTH}`.

`CAS_STRENGTH` is one local constant in `_decode_filter_args` shared by all five branches
(0.72 since #39, raised from 0.5 as near-invisible) — this memory is about where `cas` sits
in the chain, not how strong it is, so read the constant rather than trusting a number here.

Rule of thumb: a CPU filter after a GPU filter needs `hwdownload` ONLY if the GPU filter
emits hardware-surface frames. libplacebo-with-`format=` does not; scale_cuda does. Prove
any change to this chain with an actual `hevc_nvenc` encode, not just an argv assertion.
See [[project-overview]] and [[local-testing-gotchas]].
