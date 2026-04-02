"""Session debug NDJSON (Cursor debug mode) — append-only, best-effort."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_LOG = Path(__file__).resolve().parent.parent / "debug-9397a8.log"
_SESSION = "9397a8"


def agent_debug_log(
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> None:
    try:
        line = json.dumps(
            {
                "sessionId": _SESSION,
                "hypothesisId": hypothesis_id,
                "location": location,
                "message": message,
                "data": data or {},
                "timestamp": int(time.time() * 1000),
            },
            ensure_ascii=False,
        )
        with _LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
