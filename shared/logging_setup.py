"""Structured logging for staging vs production."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any


def _is_staging() -> bool:
    return os.environ.get("ENVIRONMENT", "staging").lower() == "staging"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        ed = getattr(record, "extra_data", None)
        if ed is not None:
            payload["extra"] = ed
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(service_name: str) -> None:
    level = logging.DEBUG if _is_staging() else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    for h in root.handlers[:]:
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    logging.getLogger(service_name).debug(
        "logging_initialized",
        extra={"extra_data": {"environment": os.environ.get("ENVIRONMENT", "staging")}},
    )


def log_extra(logger: logging.Logger, level: int, msg: str, **kwargs: Any) -> None:
    """Attach structured extra in staging."""
    if _is_staging():
        logger.log(level, msg, extra={"extra_data": kwargs})
    else:
        logger.log(level, msg)
