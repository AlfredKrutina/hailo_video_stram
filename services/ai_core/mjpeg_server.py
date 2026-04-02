"""
MJPEG over HTTP for Nginx upstream (multipart/x-mixed-replace).

Contract:
- One global queue fed by GStreamer jpeg branch; if empty, we spin until frames arrive (no 404).
- Clients disconnecting mid-stream are normal; I/O errors during write are logged at warning.
- Path is /stream.mjpeg on ai_core:8081; Nginx rewrites public URLs (/mjpeg/, /video/, etc.).
"""

from __future__ import annotations

import asyncio
import logging
import queue
from typing import TYPE_CHECKING

from aiohttp import web

from shared.errors import ErrorCode, log_error, log_warning_code

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
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            # Client closed connection; expected under load or tab close
            log_warning_code(
                logger,
                ErrorCode.MJPEG_STREAM_ERROR,
                "mjpeg client disconnected",
                err=str(e),
            )
        except Exception as e:
            log_error(
                logger,
                ErrorCode.MJPEG_STREAM_ERROR,
                "mjpeg stream aborted",
                exc=e,
            )
        return resp

    app.router.add_get("/stream.mjpeg", stream)
    return app


async def run_mjpeg_server(host: str, port: int, q: queue.Queue[bytes | None]) -> None:
    """Bind aiohttp; runs until process exit (cancelled by main thread)."""
    app = create_mjpeg_app(q)
    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info("mjpeg_listen", extra={"extra_data": {"host": host, "port": port}})
        await asyncio.Future()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log_error(logger, ErrorCode.MJPEG_STREAM_ERROR, "mjpeg server failed to start", exc=e)
        raise
