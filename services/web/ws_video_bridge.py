"""
Jedno sdílené spojení web → ai_core WebSocket (/ws/video); poslední binární rámec pro forward do prohlížeče.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import aiohttp

logger = logging.getLogger("web.video_bridge")

AI_CORE_VIDEO_WS_URL = os.environ.get("RPY_AI_CORE_VIDEO_WS_URL", "ws://ai_core:8081/ws/video")

_last_video_message: Optional[bytes] = None
_lock = asyncio.Lock()
_ingest_task: Optional[asyncio.Task[None]] = None


async def get_last_video_frame() -> Optional[bytes]:
    async with _lock:
        return _last_video_message


async def _ingest_loop() -> None:
    global _last_video_message
    backoff = 1.0
    while True:
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=300)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.ws_connect(AI_CORE_VIDEO_WS_URL, heartbeat=60) as ws:
                    backoff = 1.0
                    logger.info("ai_core_video_ws_connected", extra={"extra_data": {"url": AI_CORE_VIDEO_WS_URL}})
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            async with _lock:
                                _last_video_message = msg.data
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "ai_core_video_ws_reconnect",
                extra={"extra_data": {"err": str(e), "backoff_s": round(backoff, 2)}},
            )
            async with _lock:
                _last_video_message = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 30.0)


def start_video_ingest() -> None:
    global _ingest_task
    if _ingest_task is not None and not _ingest_task.done():
        return
    _ingest_task = asyncio.create_task(_ingest_loop(), name="ai-core-video-ingest")
