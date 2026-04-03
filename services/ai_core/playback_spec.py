"""Playback source description for GStreamer (direct URI vs yt-dlp stdout pipe)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PlaybackSpec:
    """
    - direct: open `uri` with playbin / uridecodebin.
    - ytdlp_pipe: stream via `yt-dlp -o -` into fdsrc → decodebin (avoids googlevideo 403 on souphttpsrc).
    - v4l2: lokální kamera přes `v4l2src` (URI `v4l2:///dev/video0`).
    - idle: žádný externí zdroj — `videotestsrc` / idle rámec (např. po chybě yt-dlp resolve).
    """

    kind: Literal["direct", "ytdlp_pipe", "v4l2", "idle"]
    uri: str | None = None
    ytdlp_page_url: str | None = None
    v4l2_device: str | None = None
    idle_message: str | None = None
