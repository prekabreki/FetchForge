"""SSE event builders — the single source of truth for the wire format.

Every event is `data: {json}\n\n`. Centralizing this kills the f-string-vs-.format
split that the server used to carry (the f-string-backslash gotcha is avoided
here once, in _sse) and gives each event type one definition. The UI splits
`phase` msgs on ": " to separate header from filename — sse_phase enforces
that contract.
"""
import json


def _sse(obj: dict) -> str:
    return "data: {}\n\n".format(json.dumps(obj))


# Each event type is defined once as a plain dict; `Job.emit` logs that dict and
# serializes it, and the `sse_*` helpers below are the wire-only view of the same
# dict (kept because other call sites and the wire-format tests use them).
def _ev_log(msg: str) -> dict:
    return {"type": "log", "msg": msg}


def _ev_error(msg: str) -> dict:
    return {"type": "error", "msg": msg}


def _ev_cookie_warning(msg: str) -> dict:
    return {"type": "cookie_warning", "msg": msg}


def _ev_cancelled(msg: str = "Cancelled by user.") -> dict:
    return {"type": "cancelled", "msg": msg}


def _ev_done(msg: str = "All done!") -> dict:
    return {"type": "done", "msg": msg}


def _ev_shutdown(msg: str = "Shutting down in 60 seconds...") -> dict:
    return {"type": "shutdown", "msg": msg}


def _ev_phase(header: str, filename: str = "") -> dict:
    # UI contract: "header: filename" — split on the first ": " in index.html.
    msg = "{}: {}".format(header, filename) if filename else header
    return {"type": "phase", "msg": msg}


def _ev_video_start(current: int, total: int) -> dict:
    return {"type": "video_start", "current": current, "total": total}


def _ev_source_size(size_bytes: int) -> dict:
    return {"type": "source_size", "bytes": size_bytes}


def _ev_progress(pct: float, overall: float, speed: str, eta: str, size: str, output_bytes: int = 0) -> dict:
    return {"type": "progress", "pct": pct, "overall": overall,
            "speed": speed, "eta": eta, "size": size, "output_bytes": output_bytes}


def _ev_dl_progress(pct: float, size: str, speed: str, eta: str, idx: int, total: int) -> dict:
    return {"type": "dl_progress", "pct": pct, "size": size,
            "speed": speed, "eta": eta, "idx": idx, "total": total}


def _ev_item_done(idx: int, total: int) -> dict:
    return {"type": "item_done", "idx": idx, "total": total}


def _ev_item_failed(idx: int, total: int, msg: str) -> dict:
    return {"type": "item_failed", "idx": idx, "total": total, "msg": msg}


def _ev_size_info(source_bytes: int, output_bytes: int) -> dict:
    bloat = (output_bytes - source_bytes) / source_bytes * 100 if source_bytes > 0 else 0.0
    return {"type": "size_info",
            "source_mb": round(source_bytes / 1024**2, 1),
            "output_mb": round(output_bytes / 1024**2, 1),
            "bloat_pct": round(bloat, 1)}


def _ev_encode_params(params: dict) -> dict:
    return {"type": "encode_params",
            "cq": params["cq"],
            "maxrate": params["maxrate"],
            "preset": params["preset"],
            "tune": params["tune"],
            "pix_fmt": params["pix_fmt"]}


def sse_log(msg: str) -> str:
    return _sse(_ev_log(msg))


def sse_error(msg: str) -> str:
    return _sse(_ev_error(msg))


def sse_cookie_warning(msg: str) -> str:
    return _sse(_ev_cookie_warning(msg))


def sse_cancelled(msg: str = "Cancelled by user.") -> str:
    return _sse(_ev_cancelled(msg))


def sse_done(msg: str = "All done!") -> str:
    return _sse(_ev_done(msg))


def sse_shutdown(msg: str = "Shutting down in 60 seconds...") -> str:
    return _sse(_ev_shutdown(msg))


def sse_phase(header: str, filename: str = "") -> str:
    return _sse(_ev_phase(header, filename))


def sse_video_start(current: int, total: int) -> str:
    return _sse(_ev_video_start(current, total))


def sse_source_size(size_bytes: int) -> str:
    return _sse(_ev_source_size(size_bytes))


def sse_progress(pct: float, overall: float, speed: str, eta: str, size: str, output_bytes: int = 0) -> str:
    return _sse(_ev_progress(pct, overall, speed, eta, size, output_bytes))


def sse_dl_progress(pct: float, size: str, speed: str, eta: str, idx: int, total: int) -> str:
    return _sse(_ev_dl_progress(pct, size, speed, eta, idx, total))


def sse_item_done(idx: int, total: int) -> str:
    return _sse(_ev_item_done(idx, total))


def sse_item_failed(idx: int, total: int, msg: str) -> str:
    return _sse(_ev_item_failed(idx, total, msg))


def sse_size_info(source_bytes: int, output_bytes: int) -> str:
    return _sse(_ev_size_info(source_bytes, output_bytes))


def sse_encode_params(params: dict) -> str:
    return _sse(_ev_encode_params(params))
