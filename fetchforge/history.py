"""history.json persistence: one read-modify-write lock, atomic writes."""
import asyncio
import json
import os
from pathlib import Path

from . import config

_history_lock = asyncio.Lock()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text via a temp file + os.replace so a crash mid-write can't truncate
    the real file (os.replace is atomic on the same filesystem)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_history_sync() -> list:
    if config.HISTORY_PATH.exists():
        try:
            return json.loads(config.HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


async def _load_history() -> list:
    # Off the event loop — a synchronous read here jitters any in-flight SSE stream.
    return await asyncio.to_thread(_read_history_sync)


async def _save_history_entry(entry: dict):
    # Lock the whole read-modify-write so two concurrent /history POSTs can't lose
    # each other's entry; write atomically so a crash can't truncate the file.
    async with _history_lock:
        history = await asyncio.to_thread(_read_history_sync)
        history = [h for h in history if h.get("url") != entry["url"]]
        history.insert(0, entry)
        payload = json.dumps(history[:50], ensure_ascii=False, indent=2)
        await asyncio.to_thread(_atomic_write_text, config.HISTORY_PATH, payload)
