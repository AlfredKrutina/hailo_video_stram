"""
Interní WebSocket server pro JPEG snímky z GStreamer (náhrada za MJPEG HTTP).

Cesta: ``GET /ws/video`` — binární zprávy ``0x01`` + JPEG bytes, max 25 fps.
Klient: kontejner ``web`` (forward do prohlížeče).
"""

from __future__ import annotations

import asyncio
import logging
import queue
import time

from aiohttp import WSMsgType, web

from shared.errors import ErrorCode, log_error

logger = logging.getLogger("ai_core.video_ws")

FRAME_PREFIX_01 = bytes([0x01])
MAX_FPS = 25.0
MIN_INTERVAL_S = 1.0 / MAX_FPS


def _wait_latest_jpeg(q: queue.Queue[bytes | None], timeout: float) -> bytes | None:
    """Blokuje na první snímek, pak vyprázdní frontu a vrátí nejnovější."""
    try:
        first = q.get(timeout=timeout)
    except queue.Empty:
        return None
    if not first:
        return None
    latest = first
    while True:
        try:
            n = q.get_nowait()
            if n:
                latest = n
        except queue.Empty:
            break
    return latest


async def _broadcast_loop(app: web.Application) -> None:
    q: queue.Queue[bytes | None] = app["jpeg_queue"]
    clients: list[web.WebSocketResponse] = app["ws_clients"]
    ws_lock: asyncio.Lock = app["ws_lock"]
    loop = asyncio.get_running_loop()
    last_send = 0.0

    while True:
        async with ws_lock:
            has_clients = len(clients) > 0
        if not has_clients:
            await asyncio.sleep(0.25)
            continue

        now = time.monotonic()
        wait = MIN_INTERVAL_S - (now - last_send)
        if wait > 0:
            await asyncio.sleep(wait)

        chunk = await loop.run_in_executor(None, _wait_latest_jpeg, q, 0.75)
        if not chunk:
            continue

        last_send = time.monotonic()
        payload = FRAME_PREFIX_01 + chunk

        async with ws_lock:
            snapshot = list(clients)
        dead: list[web.WebSocketResponse] = []
        for ws in snapshot:
            try:
                await ws.send_bytes(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with ws_lock:
                for ws in dead:
                    if ws in clients:
                        clients.remove(ws)


async def _ws_video_handler(request: web.Request) -> web.StreamResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    app = request.app
    clients: list[web.WebSocketResponse] = app["ws_clients"]
    ws_lock: asyncio.Lock = app["ws_lock"]

    async with ws_lock:
        clients.append(ws)
        if app["broadcast_task"] is None or app["broadcast_task"].done():
            app["broadcast_task"] = asyncio.create_task(_broadcast_loop(app))

    try:
        async for msg in ws:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
    finally:
        async with ws_lock:
            if ws in clients:
                clients.remove(ws)
        try:
            await ws.close()
        except Exception:
            pass
    return ws


def create_video_ws_app(q: queue.Queue[bytes | None]) -> web.Application:
    app = web.Application()
    app["jpeg_queue"] = q
    app["ws_clients"] = []
    app["ws_lock"] = asyncio.Lock()
    app["broadcast_task"] = None
    app.router.add_get("/ws/video", _ws_video_handler)
    return app


async def run_video_ws_server(host: str, port: int, q: queue.Queue[bytes | None]) -> None:
    app = create_video_ws_app(q)
    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info("video_ws_listen", extra={"extra_data": {"host": host, "port": port, "path": "/ws/video"}})
        await asyncio.Future()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log_error(logger, ErrorCode.MJPEG_STREAM_ERROR, "video ws server failed to start", exc=e)
        raise
