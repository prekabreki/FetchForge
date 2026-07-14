"""Locate or provision an NVENC-capable ffmpeg.

FetchForge's H.265 encode uses hardware `hevc_nvenc`; a generic ffmpeg without
`--enable-nvenc` cannot run it, so presence alone is not enough — we verify the
encoder is actually listed. On Windows we can download a full build; on Linux the
build is driver/distro-coupled, so we guide the user instead.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.request import urlretrieve

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"

APPDATA_DIR = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "FetchForge"
FFMPEG_CACHE = APPDATA_DIR / "ffmpeg"


class ProvisionError(RuntimeError):
    """Raised when a working NVENC ffmpeg cannot be found or provisioned."""


def ffmpeg_has_nvenc(ffmpeg: str) -> bool:
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return False
    return "hevc_nvenc" in out.stdout


def _cached_ffmpeg() -> str | None:
    exe = FFMPEG_CACHE / ("ffmpeg.exe" if IS_WINDOWS else "ffmpeg")
    return str(exe) if exe.exists() else None


def find_nvenc_ffmpeg() -> str | None:
    candidates = [_cached_ffmpeg(), shutil.which("ffmpeg")]
    for cand in candidates:
        if cand and ffmpeg_has_nvenc(cand):
            return cand
    return None


def provision_windows(download=urlretrieve) -> str:
    """Download + unpack a full ffmpeg build into FFMPEG_CACHE, return ffmpeg.exe."""
    FFMPEG_CACHE.mkdir(parents=True, exist_ok=True)
    archive = FFMPEG_CACHE / "ffmpeg-release-full.zip"
    try:
        download(_windows_zip_url(), archive)
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                name = Path(member).name
                if name in ("ffmpeg.exe", "ffprobe.exe"):
                    with zf.open(member) as src, open(FFMPEG_CACHE / name, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    except (OSError, zipfile.BadZipFile) as e:
        raise ProvisionError(
            f"Failed to download or unpack ffmpeg for Windows: {e}\n"
            "Install ffmpeg manually (put ffmpeg.exe/ffprobe.exe on PATH) and re-run."
        ) from e
    finally:
        archive.unlink(missing_ok=True)
    exe = FFMPEG_CACHE / "ffmpeg.exe"
    if not exe.exists():
        raise ProvisionError("Downloaded ffmpeg archive did not contain ffmpeg.exe")
    return str(exe)


def _windows_zip_url() -> str:
    # BtbN's FFmpeg-Builds GPL "latest" build (github.com/BtbN/FFmpeg-Builds):
    # ships as a .zip (no extra deps to unpack) and includes hevc_nvenc.
    return ("https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/"
            "ffmpeg-master-latest-win64-gpl.zip")


def linux_guidance() -> str:
    return (
        "FetchForge needs an NVENC-capable ffmpeg and the NVIDIA proprietary driver.\n"
        "Install ffmpeg from your package manager, e.g.:\n"
        "  Fedora/Nobara: sudo dnf install ffmpeg\n"
        "  Debian/Ubuntu: sudo apt install ffmpeg\n"
        "Then re-run `fetchforge`."
    )


def macos_guidance() -> str:
    return (
        "FetchForge's H.265 transcode requires an NVIDIA GPU with NVENC, which "
        "macOS does not provide. The download + audio-extract features still work, "
        "but the video transcode cannot run on this platform."
    )


def _use(ffmpeg: str) -> str:
    os.environ["PATH"] = str(Path(ffmpeg).parent) + os.pathsep + os.environ.get("PATH", "")
    return ffmpeg


def ensure_ffmpeg() -> str:
    existing = find_nvenc_ffmpeg()
    if existing:
        return _use(existing)
    if IS_WINDOWS:
        return _use(provision_windows())
    if IS_MACOS:
        raise ProvisionError(macos_guidance())
    raise ProvisionError(linux_guidance())
