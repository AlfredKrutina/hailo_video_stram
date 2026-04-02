"""
Structured error codes and logging helpers for raspberry_py_ajax.

Convention:
- Use `ErrorCode` in API JSON bodies (`detail` or top-level `code`) so clients and logs correlate.
- Call `log_error()` for service-level failures; it always attaches `code` to structured extra (staging JSON logs).
- Do not put secrets or full URLs with credentials in user-facing `message`; sanitize in `source_resolve.sanitize_uri`.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable identifiers for metrics, clients, and log queries."""

    REDIS_UNAVAILABLE = "REDIS_UNAVAILABLE"
    REDIS_COMMAND_FAILED = "REDIS_COMMAND_FAILED"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    DATABASE_WRITE_FAILED = "DATABASE_WRITE_FAILED"
    DATABASE_READ_FAILED = "DATABASE_READ_FAILED"
    CONFIG_INVALID = "CONFIG_INVALID"
    SOURCE_RESOLVE_FAILED = "SOURCE_RESOLVE_FAILED"
    GST_PIPELINE_ERROR = "GST_PIPELINE_ERROR"
    MJPEG_STREAM_ERROR = "MJPEG_STREAM_ERROR"
    INTERNAL = "INTERNAL"


def log_error(
    logger: logging.Logger,
    code: ErrorCode | str,
    message: str,
    *,
    exc: BaseException | None = None,
    **extra: Any,
) -> None:
    """
    Log a logical error with optional exception chain for JsonFormatter / Sentry-style review.
    `extra` is merged into extra_data (staging JSON logs).
    """
    payload = {"code": str(code), "message": message, **extra}
    if exc is not None:
        logger.error(
            f"{message}: {exc}",
            extra={"extra_data": {**payload, "exc_type": type(exc).__name__}},
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    else:
        logger.error(message, extra={"extra_data": payload})


def log_warning_code(
    logger: logging.Logger,
    code: ErrorCode | str,
    message: str,
    **extra: Any,
) -> None:
    logger.warning(message, extra={"extra_data": {"code": str(code), **extra}})


def json_loads_safe(raw: str | None, logger: logging.Logger, context: str) -> dict[str, Any]:
    """Parse JSON from Redis; on failure log and return {}."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log_warning_code(
            logger,
            ErrorCode.CONFIG_INVALID,
            f"invalid json in {context}",
            err=str(e),
        )
        return {}
