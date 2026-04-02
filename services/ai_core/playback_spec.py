"""Playback source description for GStreamer (direct URI vs yt-dlp stdout pipe)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PlaybackSpec:
    """
    - direct: open `uri` with playbin / uridecodebin.
    - ytdlp_pipe: stream via `yt-dlp -o -` into fdsrc → decodebin (avoids googlevideo 403 on souphttpsrc).
    """

    kind: Literal["direct", "ytdlp_pipe"]
    uri: str | None = None
    ytdlp_page_url: str | None = None
