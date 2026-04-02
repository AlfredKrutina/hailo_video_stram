"""
MJPEG over HTTP for Nginx upstream (multipart/x-mixed-replace).

Contract:
- One global queue fed by GStreamer jpeg branch; if empty, we spin until frames arrive (no 404).
- Clients disconnecting mid-stream are normal; I/O errors during write are logged at warning.
- Path is /stream.mjpeg on ai_core:8081; Nginx rewrites public URLs (/mjpeg/, /video/, etc.).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import queue
from typing import TYPE_CHECKING

from aiohttp import web

from shared.agent_debug_ndjson import agent_debug_log
from shared.errors import ErrorCode, log_error, log_warning_code

if TYPE_CHECKING:
    pass

logger = logging.getLogger("ai_core.mjpeg")

# 1×1 JPEG — při dlouhé prodlevě bez snímku z pipeline pošleme placeholder (prohlížeč nedostane „visící“ chunked stream).
_PLACEHOLDER_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAG/AP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAQUCf//EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQMBAT8Bf//EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQIBAT8Bf//Z"
)


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
        # Okamžitý první chunk — prohlížeč/Nginx nedostanou „visící“ chunked spojení bez těla
        # (sníží ERR_INCOMPLETE_CHUNKED_ENCODING při pomalém náběhu RTSP).
        await resp.write(boundary + _PLACEHOLDER_JPEG + b"\r\n")
        loop = asyncio.get_running_loop()

        def _get_chunk() -> bytes | None:
            try:
                return q.get(timeout=1.0)
            except queue.Empty:
                return None

        empty_s = 0
        # Kratší než dříve 5 s — při záseku pipeline držet multipart živý.
        placeholder_after_s = 2
        first_placeholder_logged = False

        try:
            while True:
                chunk = await loop.run_in_executor(None, _get_chunk)
                if chunk is None:
                    empty_s += 1
                    if empty_s >= placeholder_after_s:
                        empty_s = 0
                        # region agent log
                        if not first_placeholder_logged:
                            first_placeholder_logged = True
                            agent_debug_log(
                                "H1",
                                "mjpeg_server.py:stream",
                                "mjpeg_placeholder_no_pipeline_frames",
                                {"empty_s_before_send": placeholder_after_s},
                            )
                        # endregion
                        await resp.write(boundary + _PLACEHOLDER_JPEG + b"\r\n")
                    continue
                empty_s = 0
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
