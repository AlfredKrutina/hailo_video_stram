"""
Resolve user-facing media URIs for GStreamer.

- **normalize_media_url**: adds `https://` for pasted links without a scheme (`youtu.be/...?si=...`, etc.).
- **Known portal hosts** (YouTube, Vimeo, Twitch, …): use `PlaybackSpec(kind=ytdlp_pipe)` — data is fed via
  `yt-dlp -o -` into `fdsrc` so we never pass fragile `googlevideo.com` URLs through `souphttpsrc` (403).
- **Unknown HTTPS pages**: yt-dlp `--get-url` once; on success use **direct** URL; on “unsupported URL” passthrough.
- **Direct files** (`*.mp4`, …): `PlaybackSpec(direct)`.
- `file://` / RTSP: `PlaybackSpec(direct)`.

Use `resolve_playback()` for the full contract; `resolve_playback_uri()` remains for backward compatibility.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from urllib.parse import unquote, urlparse

from services.ai_core.playback_spec import PlaybackSpec

logger = logging.getLogger("ai_core.source_resolve")

# Hostnames we resolve via yt-dlp --get-url (not exhaustive; yt-dlp supports more via fallback).
_YTDLP_HOST_SUFFIXES: tuple[str, ...] = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "vimeo.com",
    "dailymotion.com",
    "twitch.tv",
    "tiktok.com",
    "facebook.com",
    "instagram.com",
    "reddit.com",
    "bilibili.com",
    "nicovideo.jp",
)

# Direct file-ish HTTP paths: let GStreamer open without yt-dlp.
_DIRECT_MEDIA_SUFFIXES: tuple[str, ...] = (
    ".mp4",
    ".webm",
    ".mkv",
    ".mov",
    ".avi",
    ".m4v",
    ".m3u8",
    ".mpd",
    ".ogv",
)


def normalize_media_url(raw: str) -> str:
    """
    Accept pasted links without scheme (e.g. youtu.be/xxx?si=...), protocol-relative //..., whitespace.
    """
    s = raw.strip()
    if not s:
        return s
    if s.startswith("//"):
        return "https:" + s
    if "://" in s:
        return s
    # No scheme: common paste forms
    low = s.lower()
    if any(
        h in low
        for h in (
            "youtu.be/",
            "youtube.com/",
            "youtube.com/watch",
            "vimeo.com/",
            "dailymotion.com/",
            "twitch.tv/",
            "tiktok.com/",
        )
    ):
        return "https://" + s.lstrip("/")
    return s


def _looks_like_direct_http_media(url: str) -> bool:
    try:
        p = urlparse(url)
        path = unquote(p.path or "").lower()
        return any(path.endswith(ext) for ext in _DIRECT_MEDIA_SUFFIXES)
    except Exception:
        return False


def _host_matches_ytdlp(host: str) -> bool:
    h = (host or "").lower()
    if not h:
        return False
    for suf in _YTDLP_HOST_SUFFIXES:
        if h == suf or h.endswith("." + suf):
            return True
    return False


def _should_resolve_with_ytdlp(url: str) -> bool:
    """True if URL should be passed to yt-dlp (YouTube short links, query params like ?si=..., etc.)."""
    try:
        p = urlparse(url)
        if (p.scheme or "").lower() not in ("http", "https"):
            return False
        if _looks_like_direct_http_media(url):
            return False
        return _host_matches_ytdlp(p.hostname or "")
    except Exception:
        return False


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


def _resolve_youtube(url: str) -> tuple[str | None, str | None]:
    """Resolve a page URL to a direct media URL using yt-dlp (YouTube, Vimeo, Twitch, …)."""
    try:
        ytdlp = shutil.which("yt-dlp")
        if not ytdlp:
            return None, (
                "yt-dlp: binary missing in container. "
                "Install yt-dlp in Dockerfile.ai or use a direct HTTP/RTSP/file URL."
            )
        # Single progressive URL for GStreamer: avoid strict ext=mp4 (often unavailable on YouTube).
        cmd = [
            ytdlp,
            "-f",
            "best[height<=720]/best[height<=1080]/best/worst",
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
            return None, "yt-dlp: timeout (120s) — check network from container."
        except OSError as e:
            return None, f"yt-dlp: failed to run: {e}"

        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if r.returncode != 0:
            tail = (err or out)[:800]
            return None, f"yt-dlp failed (exit {r.returncode}): {tail}"

        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("http://") or line.startswith("https://"):
                logger.info(
                    "ytdlp_resolved",
                    extra={"extra_data": {"page": sanitize_uri(url)}},
                )
                return line, None

        return None, "yt-dlp did not return a direct HTTP(S) URL."
    except Exception as e:
        logger.exception("resolve_youtube_unexpected")
        return None, f"yt-dlp resolve výjimka: {e}"


def _try_ytdlp_generic(url: str) -> tuple[str | None, str | None]:
    """
    Last resort: yt-dlp for HTTPS pages that are not in _YTDLP_HOST_SUFFIXES but might still work
    (new sites, embeds). Skipped for obvious direct files.
    """
    if _looks_like_direct_http_media(url):
        return None, None
    try:
        return _resolve_youtube(url)
    except Exception as e:
        logger.exception("try_ytdlp_generic")
        return None, str(e)


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


def resolve_playback(
    configured_uri: str,
    *,
    idle_message: str | None = None,
) -> tuple[PlaybackSpec | None, str | None]:
    """
    Returns (PlaybackSpec, None) on success, or (None, error_message).
    Interní URI `rpy-internal:idle` přepne pipeline na idle (videotestsrc) — po pádu YouTube atd.
    """
    raw = normalize_media_url((configured_uri or "").strip())
    if not raw:
        return None, "Prázdná URL zdroje."

    if raw.strip() in ("rpy-internal:idle", "idle://"):
        return PlaybackSpec(
            kind="idle",
            idle_message=idle_message or "Zdroj nedostupný — přepnuto na idle náhled.",
        ), None

    if raw.lower().startswith("v4l2://"):
        parsed = urlparse(raw)
        dev = unquote(parsed.path or "/dev/video0")
        if not dev.startswith("/"):
            dev = "/dev/" + dev.lstrip("/")
        logger.info(
            "playback_v4l2",
            extra={"extra_data": {"device": dev}},
        )
        return PlaybackSpec(kind="v4l2", v4l2_device=dev), None

    resolved, err_f = _resolve_file(raw)
    if err_f:
        return None, err_f

    u = resolved or raw
    if _should_resolve_with_ytdlp(u):
        logger.info(
            "playback_ytdlp_pipe",
            extra={"extra_data": {"page": sanitize_uri(u)}},
        )
        return PlaybackSpec(kind="ytdlp_pipe", ytdlp_page_url=u), None

    if u.lower().startswith(("http://", "https://")) and not _looks_like_direct_http_media(u):
        try:
            direct, yerr = _try_ytdlp_generic(u)
        except Exception as e:
            logger.exception("ytdlp_generic_resolve_exception")
            return PlaybackSpec(
                kind="idle",
                idle_message=f"yt-dlp resolve výjimka: {e}"[:800],
            ), None
        if direct:
            return PlaybackSpec(kind="direct", uri=direct), None
        if yerr and "unsupported url" not in yerr.lower():
            # Místo tvrdé chyby — idle generátor + důvod v telemetrii / Redis
            return PlaybackSpec(kind="idle", idle_message=yerr[:1200]), None
        logger.debug(
            "source_http_passthrough_after_ytdlp_skip",
            extra={"extra_data": {"uri": sanitize_uri(u)}},
        )

    if u.lower().startswith("rtsp://"):
        logger.debug(
            "source_rtsp_direct",
            extra={"extra_data": {"uri": sanitize_uri(u)}},
        )
    elif u.lower().startswith(("http://", "https://")):
        logger.debug(
            "source_http_direct",
            extra={"extra_data": {"uri": sanitize_uri(u)}},
        )

    return PlaybackSpec(kind="direct", uri=u), None


def resolve_playback_uri(configured_uri: str) -> tuple[str | None, str | None]:
    """
    Legacy: returns (uri, err) for direct playback only.
    For ytdlp_pipe sources returns (page_url, None) — prefer `resolve_playback`.
    """
    spec, err = resolve_playback(configured_uri)
    if err or not spec:
        return None, err
    if spec.kind == "direct":
        return spec.uri, None
    if spec.kind == "v4l2":
        return spec.v4l2_device, None
    if spec.kind == "idle":
        return "idle://", None
    return spec.ytdlp_page_url, None
