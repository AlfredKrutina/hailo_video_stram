"""
FastAPI service (`web` container): REST, WebSocket, static SPA.

Architecture (see also nginx/nginx.conf):
- Browser talks to Nginx :80. `/`, `/assets`, `/api/*`, `/ws/*` are proxied here.
- Live video: JPEG přes `/ws/telemetry` (binární rámce z ai_core přes [ws_video_bridge](services/web/ws_video_bridge.py)).
- Redis holds hot state (telemetry, detections, config:* keys). PostgreSQL holds recording policy rows and events.
- If Redis is down, prefer HTTP 503 with `{"code":"REDIS_UNAVAILABLE",...}` (see shared.errors.ErrorCode).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

import redis
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from shared.errors import ErrorCode, json_loads_safe, log_error, log_warning_code
from shared.logging_setup import setup_logging
from shared.schemas.config import ModelConfig, SourceConfig
from shared.schemas.recording import RecordingPolicy, default_catalog, default_policy
from services.persistence.recording_store import (
    list_events,
    load_policy_from_db,
    policy_to_redis_json,
    save_policy_to_db,
)
from services.persistence.session import get_database_url, init_db
from services.web.diagnostics import collect_diagnostics
from services.web.recording_api import validate_policy_against_catalog
from services.web.ws_video_bridge import get_last_video_frame, start_video_ingest

logger = logging.getLogger("web")

T = TypeVar("T")

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/data/snapshots")
STATIC_DIR = Path(__file__).resolve().parent / "static"

r = redis.from_url(REDIS_URL, decode_responses=True)

app = FastAPI(title="raspberry_py_ajax", version="0.1.0")

_db_ok = False


@app.middleware("http")
async def log_requests_and_guard(request: Request, call_next: Any) -> Any:
    """
    Log every HTTP request with duration + status (logger name `web`, message `http_request`).
    Turn uncaught non-HTTP exceptions into JSON 500 so the SPA never gets an empty body.
    """
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except StarletteHTTPException:
        raise
    except Exception as exc:
        log_error(
            logger,
            ErrorCode.INTERNAL,
            f"unhandled exception on {request.method} {request.url.path}",
            exc=exc,
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": ErrorCode.INTERNAL.value,
                "message": "Internal server error",
            },
        )
    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
    logger.info(
        "http_request",
        extra={
            "extra_data": {
                "method": request.method,
                "path": request.url.path,
                "status": getattr(response, "status_code", None),
                "ms": elapsed_ms,
            },
        },
    )
    return response


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    log_warning_code(
        logger,
        ErrorCode.CONFIG_INVALID,
        "request validation failed",
        path=str(request.url.path),
        errors=exc.errors(),
    )
    return JSONResponse(
        status_code=422,
        content={
            "code": ErrorCode.CONFIG_INVALID.value,
            "message": "Request validation failed",
            "errors": exc.errors(),
        },
    )


def _redis_call(fn: Callable[[], T], *, op: str) -> T:
    """Run a Redis callable; on failure log + translate to 503 JSON for HTTP routes."""
    try:
        return fn()
    except redis.RedisError as e:
        log_error(logger, ErrorCode.REDIS_COMMAND_FAILED, op, exc=e)
        raise HTTPException(
            status_code=503,
            detail={
                "code": ErrorCode.REDIS_UNAVAILABLE.value,
                "message": "Redis unavailable",
            },
        ) from e


@app.on_event("startup")
async def startup() -> None:
    global _db_ok
    setup_logging("web")
    if get_database_url():
        try:
            db_ready = init_db()
        except Exception as e:
            # init_db() může vyhodit (DB ještě nedostupná, síť) — web musí přesto naběhnout (Redis + API).
            log_error(logger, ErrorCode.DATABASE_UNAVAILABLE, "init_db failed", exc=e)
            _db_ok = False
        else:
            if not db_ready:
                logger.warning("database_init_returned_false")
                _db_ok = False
            else:
                try:
                    p = load_policy_from_db()
                    if p is not None:
                        try:
                            r.set("config:recording_policy", policy_to_redis_json(p))
                            r.publish(
                                "config:updates",
                                json.dumps({"type": "recording_policy", "payload": p.model_dump()}),
                            )
                        except redis.RedisError as e:
                            log_error(logger, ErrorCode.REDIS_COMMAND_FAILED, "startup redis seed", exc=e)
                            _db_ok = False
                        else:
                            _db_ok = True
                    else:
                        # DB reachable but no policy row yet — still treat Postgres as usable
                        _db_ok = True
                except SQLAlchemyError as e:
                    log_error(logger, ErrorCode.DATABASE_READ_FAILED, "db seed read failed", exc=e)
                    _db_ok = False
                except Exception as e:
                    log_error(logger, ErrorCode.DATABASE_READ_FAILED, "db_seed_failed", exc=e)
                    _db_ok = False
    else:
        logger.warning("database_url_missing_skipping_pg")
    try:
        start_video_ingest()
    except Exception as e:
        logger.warning(
            "video_ingest_start_failed_continuing",
            extra={"extra_data": {"err": str(e)}},
        )


@app.exception_handler(redis.RedisError)
async def handle_redis_global(request: Request, exc: redis.RedisError) -> JSONResponse:
    """Fallback for any Redis error not caught inside a route (e.g. new endpoints)."""
    log_error(
        logger,
        ErrorCode.REDIS_UNAVAILABLE,
        "unhandled redis error",
        exc=exc,
        path=str(request.url.path),
    )
    return JSONResponse(
        status_code=503,
        content={
            "code": ErrorCode.REDIS_UNAVAILABLE.value,
            "message": "Redis unavailable",
        },
    )


class ModelPatch(BaseModel):
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    iou_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class SourcePatch(BaseModel):
    uri: str
    label: str | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness: `status` ok only if Redis answers; Postgres field reflects seed/migrations state."""
    out: dict[str, Any] = {"status": "ok", "service": "web"}
    try:
        r.ping()
        out["redis"] = "ok"
    except redis.RedisError as e:
        log_error(logger, ErrorCode.REDIS_COMMAND_FAILED, "health ping", exc=e)
        raise HTTPException(
            status_code=503,
            detail={
                "code": ErrorCode.REDIS_UNAVAILABLE.value,
                "message": str(e),
            },
        ) from e
    if get_database_url():
        out["postgres"] = "ok" if _db_ok else "degraded"
    else:
        out["postgres"] = "skipped"
    return out


@app.get("/api/v1/recording/catalog")
def recording_catalog() -> dict[str, Any]:
    return default_catalog().model_dump()


@app.get("/api/v1/recording/policy")
def recording_policy_get() -> dict[str, Any]:
    if not get_database_url() or not _db_ok:
        return {"policy": default_policy().model_dump(), "source": "default"}
    try:
        p = load_policy_from_db()
    except SQLAlchemyError as e:
        log_error(logger, ErrorCode.DATABASE_READ_FAILED, "load_policy_from_db", exc=e)
        return {
            "policy": default_policy().model_dump(),
            "source": "error",
            "error_code": ErrorCode.DATABASE_READ_FAILED.value,
        }
    if p is None:
        return {"policy": default_policy().model_dump(), "source": "default"}
    return {"policy": p.model_dump(), "source": "database"}


@app.put("/api/v1/recording/policy")
def recording_policy_put(body: RecordingPolicy) -> dict[str, Any]:
    if not get_database_url() or not _db_ok:
        raise HTTPException(
            503,
            detail={
                "code": ErrorCode.DATABASE_UNAVAILABLE.value,
                "message": "PostgreSQL is not available",
            },
        )
    cat = default_catalog()
    validate_policy_against_catalog(body, cat)
    try:
        save_policy_to_db(body)
    except SQLAlchemyError as e:
        log_error(logger, ErrorCode.DATABASE_WRITE_FAILED, "save_policy_to_db", exc=e)
        raise HTTPException(
            503,
            detail={
                "code": ErrorCode.DATABASE_WRITE_FAILED.value,
                "message": "Failed to save policy",
            },
        ) from e
    _redis_call(lambda: r.set("config:recording_policy", policy_to_redis_json(body)), op="set recording policy")
    _redis_call(
        lambda: r.publish(
            "config:updates",
            json.dumps({"type": "recording_policy", "payload": body.model_dump()}),
        ),
        op="publish recording policy",
    )
    return {"ok": True, "policy": body.model_dump()}


@app.get("/api/v1/detections/latest")
def detections_latest() -> Any:
    raw = _redis_call(lambda: r.get("detections:latest"), op="get detections:latest")
    return json_loads_safe(raw, logger, "detections:latest")


@app.get("/api/v1/telemetry")
def telemetry() -> Any:
    raw = _redis_call(lambda: r.get("telemetry:latest"), op="get telemetry:latest")
    return json_loads_safe(raw, logger, "telemetry:latest")


@app.get("/api/v1/diagnostics")
async def diagnostics() -> Any:
    """Aggregated checks for operators (Redis, DB, ai_core, video WebSocket upstream)."""
    try:
        report = await collect_diagnostics(
            r,
            db_ok=_db_ok,
            environment=os.environ.get("ENVIRONMENT"),
        )
    except redis.RedisError as e:
        log_error(logger, ErrorCode.REDIS_COMMAND_FAILED, "diagnostics", exc=e)
        raise HTTPException(
            status_code=503,
            detail={
                "code": ErrorCode.REDIS_UNAVAILABLE.value,
                "message": str(e),
            },
        ) from e
    return report.model_dump(mode="json")


@app.patch("/api/v1/model")
def patch_model(body: ModelPatch) -> dict[str, Any]:
    cur = ModelConfig()
    raw = _redis_call(lambda: r.get("config:model"), op="get config:model")
    if raw:
        try:
            cur = ModelConfig.model_validate_json(raw)
        except Exception as e:
            log_error(logger, ErrorCode.CONFIG_INVALID, "config:model corrupt", exc=e)
            raise HTTPException(
                422,
                detail={
                    "code": ErrorCode.CONFIG_INVALID.value,
                    "message": "Stored model config is invalid",
                },
            ) from e
    data = cur.model_dump()
    if body.confidence_threshold is not None:
        data["confidence_threshold"] = body.confidence_threshold
    if body.iou_threshold is not None:
        data["iou_threshold"] = body.iou_threshold
    new = ModelConfig.model_validate(data)
    _redis_call(lambda: r.set("config:model", new.model_dump_json()), op="set config:model")
    _redis_call(
        lambda: r.publish(
            "config:updates",
            json.dumps({"type": "model", "payload": new.model_dump()}),
        ),
        op="publish model",
    )
    return {"ok": True, "model": new.model_dump()}


@app.patch("/api/v1/source")
def patch_source(body: SourcePatch) -> dict[str, Any]:
    src = SourceConfig(uri=body.uri, label=body.label or "custom")
    payload = json.dumps({"uri": src.uri, "label": src.label})
    _redis_call(lambda: r.set("config:source", payload), op="set config:source")
    _redis_call(
        lambda: r.publish(
            "config:updates",
            json.dumps({"type": "source", "payload": src.model_dump()}),
        ),
        op="publish source",
    )
    return {"ok": True, "source": src.model_dump(), "state": "SWITCHING"}


@app.get("/api/v1/events")
def events_list(
    count: int = 50,
    offset: int = 0,
    label: str | None = None,
    source: str = "database",
) -> dict[str, Any]:
    if source == "redis":
        try:
            rows = r.xrevrange("events:detections", count=count)
        except redis.RedisError as e:
            log_warning_code(
                logger,
                ErrorCode.REDIS_COMMAND_FAILED,
                "events_list xrevrange (redis mode)",
                err=str(e),
            )
            return {"events": [], "source": "redis", "error": "redis_failed"}
        out = []
        for eid, fields in rows:
            out.append({"id": eid, **fields})
        return {"events": out, "source": "redis"}

    if not _db_ok:
        try:
            rows = r.xrevrange("events:detections", count=count)
        except redis.RedisError as e:
            log_warning_code(
                logger,
                ErrorCode.REDIS_COMMAND_FAILED,
                "events_list xrevrange (db degraded)",
                err=str(e),
            )
            return {"events": [], "source": "redis_fallback", "error": "redis_failed"}
        out = []
        for eid, fields in rows:
            out.append({"id": eid, **fields})
        return {"events": out, "source": "redis_fallback", "note": "postgres unavailable"}

    try:
        events = list_events(limit=count, offset=offset, label=label.lower() if label else None)
    except SQLAlchemyError as e:
        log_error(logger, ErrorCode.DATABASE_READ_FAILED, "list_events", exc=e)
        raise HTTPException(
            503,
            detail={
                "code": ErrorCode.DATABASE_READ_FAILED.value,
                "message": "Failed to list events",
            },
        ) from e
    return {"events": events, "source": "database"}


@app.get("/api/v1/snapshots/{name}")
def get_snapshot(name: str) -> FileResponse:
    base = Path(SNAPSHOT_DIR).resolve()
    path = (base / name).resolve()
    if not str(path).startswith(str(base)) or not path.is_file():
        logger.debug("snapshot_not_found", extra={"extra_data": {"name": name[:200]}})
        raise HTTPException(
            404,
            detail={"code": "NOT_FOUND", "message": "Snapshot not found"},
        )
    return FileResponse(path, media_type="image/jpeg")


@app.websocket("/ws/telemetry")
async def ws_telemetry(ws: WebSocket) -> None:
    """
    Telemetrie + detekce jako JSON (~4 Hz) a video z ai_core jako binární zprávy (prefix 0x01 + JPEG).
    Jedna smyčka — nedochází k souběžnému send na stejném WS.
    """
    await ws.accept()
    redis_fail_streak = 0
    t_next_json = time.perf_counter()
    t_next_video = time.perf_counter()
    prev_video: bytes | None = None
    json_interval = 0.25
    video_interval = 1.0 / 25.0

    try:
        while True:
            now = time.perf_counter()
            if now >= t_next_json:
                t_next_json = now + json_interval
                try:
                    raw, det = await asyncio.to_thread(
                        lambda: (r.get("telemetry:latest"), r.get("detections:latest")),
                    )
                    redis_fail_streak = 0
                except redis.RedisError as e:
                    redis_fail_streak += 1
                    log_error(logger, ErrorCode.REDIS_COMMAND_FAILED, "ws telemetry redis get", exc=e)
                    raw, det = None, None
                    if redis_fail_streak == 1 or redis_fail_streak % 40 == 0:
                        logger.warning(
                            "ws_telemetry_redis_degraded",
                            extra={"extra_data": {"streak": redis_fail_streak}},
                        )
                payload = {
                    "telemetry": json_loads_safe(raw, logger, "ws:telemetry"),
                    "detections": json_loads_safe(det, logger, "ws:detections"),
                }
                if redis_fail_streak:
                    payload["_meta"] = {"redis_degraded": True, "streak": redis_fail_streak}
                await ws.send_json(payload)

            if now >= t_next_video:
                frame = await get_last_video_frame()
                if frame is not None and frame != prev_video:
                    prev_video = frame
                    t_next_video = now + video_interval
                    await ws.send_bytes(frame)
                else:
                    t_next_video = now + video_interval

            await asyncio.sleep(0.01)
    except WebSocketDisconnect:
        logger.debug("ws_telemetry_disconnect")
        return
    except (RuntimeError, ConnectionError) as e:
        log_warning_code(logger, ErrorCode.INTERNAL, "ws telemetry send failed", err=str(e))
        return


@app.get("/")
async def index() -> FileResponse:
    p = STATIC_DIR / "index.html"
    if not p.is_file():
        raise HTTPException(404, "static UI missing")
    return FileResponse(p)


app.mount("/assets", StaticFiles(directory=str(STATIC_DIR)), name="assets")
