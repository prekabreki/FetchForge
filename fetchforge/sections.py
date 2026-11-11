"""Time-range (section) capture (#76) — pure helpers.

A section is (start_s, end_s) in whole seconds, end_s None = to the end of the
video. yt-dlp stream-copies just that stretch (no re-encode, so the copy starts
at the keyframe at or before start_s) and -copyts keeps YouTube's own timeline
in the file. The encode step then seeks frame-exactly to start_s and stops at
end_s — we decode every frame there anyway, so precision costs nothing, whereas
yt-dlp's --force-keyframes-at-cuts would add a slow CPU re-encode and a second
lossy generation before the NVENC one.
"""
import asyncio
from pathlib import Path

from . import ytdlp

_MAX_SECTION_SECS = 1_000_000


def parse_section(start: str, end: str):
    """(start_s, end_s|None) from the form's whole-second strings, or None when
    no range applies ("" for both, or 0-to-end). ValueError on anything else —
    these values end up in a subprocess argv."""
    s, e = (start or "").strip(), (end or "").strip()
    if not s and not e:
        return None
    for v in (s, e):
        if v and not (v.isdigit() and int(v) <= _MAX_SECTION_SECS):
            raise ValueError("time range must be whole seconds, got {!r}".format(v))
    start_s = int(s) if s else 0
    end_s = int(e) if e else None
    if end_s is not None and end_s <= start_s:
        raise ValueError("time range end ({}s) must be after its start ({}s)".format(end_s, start_s))
    if start_s == 0 and end_s is None:
        return None
    return (start_s, end_s)


def _fmt_section_time(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return "{}h{:02d}m{:02d}s".format(h, m, s) if h else "{}m{:02d}s".format(m, s)


def section_suffix(section) -> str:
    """Filename marker for a section, e.g. "_12m30s-18m00s" / "_1h02m00s-end"."""
    if not section:
        return ""
    start_s, end_s = section
    return "_{}-{}".format(_fmt_section_time(start_s),
                           _fmt_section_time(end_s) if end_s is not None else "end")


def section_ytdlp_args(section) -> list:
    """yt-dlp args to fetch only the section, timestamps preserved."""
    if not section:
        return []
    start_s, end_s = section
    return [
        "--download-sections", "*{}-{}".format(start_s, end_s if end_s is not None else "inf"),
        "--downloader-args", "ffmpeg_o:-copyts",
    ]


def section_input_seek(section, file_start_time: float) -> list:
    """ffmpeg input options that cut a section download to exactly the section.
    *file_start_time* is the file's own start (≤ start_s: the copy began at a
    keyframe), and input -ss is relative to it."""
    if not section:
        return []
    start_s, end_s = section
    args = ["-ss", "{:.3f}".format(max(0.0, start_s - (file_start_time or 0.0)))]
    if end_s is not None:
        args += ["-t", str(end_s - start_s)]
    return args


def _with_input_options(argv: list, input_opts: list) -> list:
    """Insert input options immediately before the (single) -i of an ffmpeg argv."""
    if not input_opts:
        return argv
    i = argv.index("-i")
    return argv[:i] + list(input_opts) + argv[i:]


def _resolve_section(section, full_duration: float):
    """The section's end as a number (the video's end when open-ended), or None
    if it is open-ended and the video's duration is unknown."""
    start_s, end_s = section
    return end_s if end_s is not None else (full_duration or None)


def _clip_duration(section, full_duration: float):
    """Seconds the encode will produce from a section download (drives progress)."""
    end = _resolve_section(section, full_duration)
    return float(end - section[0]) if end is not None and end > section[0] else None


async def _media_time_span(path: Path):
    """(first, last) timestamp actually present in a media file, from its packets.
    Format-level duration can't answer this: for a Matroska/WebM file with a
    nonzero start (a -copyts section) ffprobe reports the END timestamp as its
    "duration", and other containers report the length — packets mean one thing
    everywhere. Reads packet headers only (no decode). None if unreadable."""
    proc = await asyncio.create_subprocess_exec(
        ytdlp.get_ffprobe(), "-v", "error",
        "-show_entries", "packet=pts_time,duration_time",
        "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    first = last = None
    for line in out.decode(errors="replace").splitlines():
        pts, _, dur = line.partition(",")
        try:
            t = float(pts)
        except ValueError:
            continue   # N/A
        try:
            end = t + float(dur.strip(",") or 0)
        except ValueError:
            end = t
        first = t if first is None else min(first, t)
        last = end if last is None else max(last, end)
    return (first, last) if first is not None else None


async def _section_download_is_complete(path: Path, section, full_duration: float) -> bool:
    """A section download is complete when the file's own timeline (-copyts)
    covers the whole requested range, ±2s. Size and the full video's duration
    say nothing about a clip, so _download_is_complete can't judge it."""
    if not path.exists():
        return False
    need_end = _resolve_section(section, full_duration)
    if need_end is None:
        return False
    span = await _media_time_span(path)
    if span is None:
        return False
    t0, t1 = span
    return t0 <= section[0] + 2.0 and t1 >= need_end - 2.0
