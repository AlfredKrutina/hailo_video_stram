"""Map user-facing source URIs to what GStreamer uridecodebin can open."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from urllib.parse import unquote, urlparse

logger = logging.getLogger("ai_core.source_resolve")

_YT_HOST = re.compile(
    r"(^|\.)youtube\.com$|(^|\.)youtu\.be$|youtube-nocookie\.com$",
    re.IGNORECASE,
)


def sanitize_uri(uri: str) -> str:
    """Hide user:pass in logs and UI."""
    if not uri or "@" not in uri or "://" not in uri:
        return uri
    try:
        scheme, rest = uri.split("://", 1)
        if "@" in rest:
            _userinfo, hostpath = rest.rsplit("@", 1)
            return f"{scheme}://***@{hostpath}"
    except Exception:
        pass
    return uri


def _is_youtube_page(uri: str) -> bool:
    try:
        p = urlparse(uri)
        host = (p.hostname or "").lower()
        if not host:
            return False
        return bool(_YT_HOST.search(host))
    except Exception:
        return False


def _resolve_youtube(url: str) -> tuple[str | None, str | None]:
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        return None, (
            "YouTube: v kontejneru chybí příkaz yt-dlp. "
            "Nainstalujte balíček yt-dlp v Dockerfile.ai nebo použijte přímý HTTP/RTSP odkaz."
        )
    cmd = [
        ytdlp,
        "-f",
        "best[height<=720][ext=mp4]/best[height<=720]/best",
        "--get-url",
        "--no-warnings",
        "--no-playlist",
        url,
    ]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "YouTube: časový limit yt-dlp (120 s) — zkontrolujte síť z kontejneru."
    except OSError as e:
        return None, f"YouTube: nelze spustit yt-dlp: {e}"

    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        tail = (err or out)[:800]
        return None, f"YouTube: yt-dlp selhalo (exit {r.returncode}): {tail}"

    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("http://") or line.startswith("https://"):
            logger.info(
                "youtube_resolved",
                extra={"extra_data": {"page": sanitize_uri(url)}},
            )
            return line, None

    return None, "YouTube: yt-dlp nevrátilo platnou HTTP URL (změna formátu stránky?)."


def _resolve_file(uri: str) -> tuple[str | None, str | None]:
    if not uri.lower().startswith("file://"):
        return uri, None
    parsed = urlparse(uri)
    path = unquote(parsed.path or "")
    if not path:
        return None, "Neplatná file:// URL (prázdná cesta)."
    if not os.path.isfile(path):
        return None, (
            f"Lokální soubor neexistuje v kontejneru: {path}. "
            "Připojte složku přes docker-compose (např. ./samples:/data/samples:ro) "
            "a použijte file:///data/samples/…"
        )
    return uri, None


def resolve_playback_uri(configured_uri: str) -> tuple[str | None, str | None]:
    """
    Returns (uri_for_gstreamer, error_message).
    If error_message is set, do not start the pipeline; show error to the user.
    """
    raw = (configured_uri or "").strip()
    if not raw:
        return None, "Prázdná URL zdroje."

    resolved, err_f = _resolve_file(raw)
    if err_f:
        return None, err_f

    if _is_youtube_page(resolved or ""):
        return _resolve_youtube(resolved or raw)

    if (resolved or "").lower().startswith("rtsp://"):
        logger.debug(
            "source_rtsp_direct",
            extra={"extra_data": {"uri": sanitize_uri(resolved or "")}},
        )
    elif (resolved or "").lower().startswith(("http://", "https://")):
        logger.debug(
            "source_http_direct",
            extra={"extra_data": {"uri": sanitize_uri(resolved or "")}},
        )

    return resolved, None
