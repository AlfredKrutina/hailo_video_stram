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
    """

    kind: Literal["direct", "ytdlp_pipe", "v4l2"]
    uri: str | None = None
    ytdlp_page_url: str | None = None
    v4l2_device: str | None = None
