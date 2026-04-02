"""Minimal MJPEG HTTP server for Nginx upstream (multipart/x-mixed-replace)."""

from __future__ import annotations

import asyncio
import logging
import queue
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    pass

logger = logging.getLogger("ai_core.mjpeg")


def create_mjpeg_app(q: queue.Queue[bytes | None]) -> web.Application:
    app = web.Application()

    async def stream(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        await resp.prepare(request)
        boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
        loop = asyncio.get_event_loop()
        def _get_chunk() -> bytes | None:
            try:
                return q.get(timeout=1.0)
            except queue.Empty:
                return None

        try:
            while True:
                chunk = await loop.run_in_executor(None, _get_chunk)
                if chunk is None:
                    continue
                await resp.write(boundary + chunk + b"\r\n")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("mjpeg_client_gone", extra={"extra_data": {"err": str(e)}})
        return resp

    app.router.add_get("/stream.mjpeg", stream)
    return app


async def run_mjpeg_server(host: str, port: int, q: queue.Queue[bytes | None]) -> None:
    app = create_mjpeg_app(q)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("mjpeg_listen", extra={"extra_data": {"host": host, "port": port}})
    await asyncio.Future()
