"""`fetchforge` console entry point: preflight ffmpeg/NVENC, then run the app."""
from __future__ import annotations

import shutil
import sys

from fetchforge import provision


def main(argv: list[str] | None = None) -> int:
    try:
        provision.ensure_ffmpeg()
    except provision.ProvisionError as e:
        if shutil.which("ffmpeg"):
            print(
                f"WARNING: {e}\n\nStarting without NVENC — YouTube download and "
                "audio-extract work, but the H.265 transcode needs an NVENC-capable "
                "ffmpeg + NVIDIA GPU.\n",
                file=sys.stderr,
            )
        else:
            print(f"FetchForge cannot start:\n\n{e}\n", file=sys.stderr)
            return 1
    # Import after preflight so tool resolution sees a provisioned ffmpeg on PATH.
    from fetchforge import server
    server.run_server(open_browser=True)
    return 0
