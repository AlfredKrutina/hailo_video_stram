"""Minimal RTSP DESCRIBE probe for link recovery (not full RTSP client)."""

from __future__ import annotations

import logging
import socket
import urllib.parse

logger = logging.getLogger("ai_core.rtsp_probe")


def rtsp_describe_ok(uri: str, timeout_s: float = 3.0) -> bool:
    if not uri.lower().startswith("rtsp://"):
        return True
    try:
        parsed = urllib.parse.urlparse(uri)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 554
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        req = (
            f"DESCRIBE {uri} RTSP/1.0\r\n"
            f"CSeq: 1\r\n"
            f"User-Agent: raspberry_py_ajax/0.1\r\n"
            f"\r\n"
        ).encode("utf-8")
        with socket.create_connection((host, port), timeout=timeout_s) as sock:
            sock.settimeout(timeout_s)
            sock.sendall(req)
            buf = sock.recv(4096)
        if b"RTSP/1.0 200" in buf or b"RTSP/1.1 200" in buf:
            return True
        logger.debug("rtsp_describe_non_200", extra={"extra_data": {"sample": buf[:200]}})
        return False
    except OSError as e:
        logger.debug("rtsp_describe_failed", extra={"extra_data": {"err": str(e)}})
        return False
