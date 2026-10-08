"""Regression test for #80: a client that drops mid-/download or mid-/convert-local
must still release the sleep inhibitor (drive _awake_refcount back to 0)."""
import asyncio
import inspect
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, mock

import anyio

from fetchforge import server


class _HangingProcess:
    """A subprocess stand-in whose communicate() never returns, so the job
    stream stays parked inside the resolve/probe await until it is cancelled.
    No real yt-dlp / ffprobe / ffmpeg is spawned."""

    returncode = 0

    async def communicate(self):
        await asyncio.Event().wait()
        return (b"", b"")


def _blocking_to_thread(fn, *args, **kwargs):
    """A stand-in for asyncio.to_thread that never returns, so the cancellation
    re-delivered on disconnect deterministically lands on the log-write await
    instead of racing a real (fast) write_text."""

    async def _block():
        await asyncio.Event().wait()
        return None

    return _block()


class WakeLockReleaseTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "logs").mkdir()
        (self.tmp / "cache").mkdir()
        (self.tmp / "out").mkdir()

        self.patches = [
            mock.patch.object(server.asyncio, "create_subprocess_exec",
                              new=mock.AsyncMock(return_value=_HangingProcess())),
            mock.patch.object(server.asyncio, "to_thread", new=_blocking_to_thread),
            mock.patch.object(server, "get_ytdlp_argv", return_value=[sys.executable]),
            mock.patch.object(server, "get_ffprobe", return_value="ffprobe"),
            mock.patch.object(server, "get_ffmpeg", return_value="ffmpeg"),
            mock.patch.object(server, "cookie_args", return_value=[]),
            mock.patch.object(server, "NODE_ARGS", []),
            mock.patch.object(server, "LOGS_DIR", self.tmp / "logs"),
            mock.patch.object(server, "CACHE_DIR", self.tmp / "cache"),
            mock.patch.object(server, "CONVERTED_DIR", self.tmp / "out"),
            mock.patch.object(server.shutil, "which", return_value=None),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

        server._awake_refcount = 0
        server._awake_inhibitor = None
        server._awake_task = None
        server._active_jobs = 0
        server._cancel_requested.clear()

    def _form_defaults(self, fn):
        kw = {}
        for name, prm in inspect.signature(fn).parameters.items():
            kw[name] = getattr(prm.default, "default", prm.default)
        return kw

    async def _cancel_mid_stream(self, body_iterator):
        """Drive the job stream as an async generator and cancel the consuming
        task while it is parked inside the generator (the same way Starlette
        cancels on client disconnect)."""

        async def main_flow():
            async with anyio.create_task_group() as tg:
                async def consume():
                    async for _chunk in body_iterator:
                        pass

                async def disconnect():
                    await anyio.sleep(0.1)
                    tg.cancel_scope.cancel()

                tg.start_soon(consume)
                tg.start_soon(disconnect)

        try:
            await main_flow()
        except BaseException:
            pass

    async def test_download_disconnect_releases_inhibitor(self):
        kw = self._form_defaults(server.download)
        kw.update(url="https://www.youtube.com/watch?v=abc", audio_format="251",
                  mode="audio", audio_preset="mp3", output_dir=str(self.tmp / "out"))
        resp = await server.download(**kw)
        await self._cancel_mid_stream(resp.body_iterator)
        self.assertEqual(server._awake_refcount, 0)

    async def test_convert_local_disconnect_releases_inhibitor(self):
        src = self.tmp / "input.mkv"
        src.write_bytes(b"not a real video")
        kw = self._form_defaults(server.convert_local)
        kw.update(files=json.dumps([str(src)]), mode="video", tune_mode="uhq",
                  output_dir=str(self.tmp / "out"))
        resp = await server.convert_local(**kw)
        await self._cancel_mid_stream(resp.body_iterator)
        self.assertEqual(server._awake_refcount, 0)
