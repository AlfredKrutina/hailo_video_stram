"""FastAPI consumer: Redis read, REST, WebSocket, static SPA, PostgreSQL events."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import redis
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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
from services.web.recording_api import validate_policy_against_catalog

logger = logging.getLogger("web")

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/data/snapshots")
STATIC_DIR = Path(__file__).resolve().parent / "static"

r = redis.from_url(REDIS_URL, decode_responses=True)

app = FastAPI(title="raspberry_py_ajax", version="0.1.0")

_db_ok = False


@app.on_event("startup")
async def startup() -> None:
    global _db_ok
    setup_logging("web")
    if get_database_url() and init_db():
        try:
            p = load_policy_from_db()
            if p is not None:
                r.set("config:recording_policy", policy_to_redis_json(p))
                r.publish(
                    "config:updates",
                    json.dumps({"type": "recording_policy", "payload": p.model_dump()}),
                )
                _db_ok = True
        except Exception as e:
            logger.error("db_seed_failed", extra={"extra_data": {"err": str(e)}})
            _db_ok = False
    else:
        logger.warning("database_url_missing_skipping_pg")


class ModelPatch(BaseModel):
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    iou_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class SourcePatch(BaseModel):
    uri: str
    label: str | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    out: dict[str, Any] = {"status": "ok"}
    try:
        r.ping()
        out["redis"] = "ok"
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
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
    p = load_policy_from_db()
    if p is None:
        return {"policy": default_policy().model_dump(), "source": "default"}
    return {"policy": p.model_dump(), "source": "database"}


@app.put("/api/v1/recording/policy")
def recording_policy_put(body: RecordingPolicy) -> dict[str, Any]:
    if not get_database_url() or not _db_ok:
        raise HTTPException(503, "PostgreSQL není dostupná")
    cat = default_catalog()
    validate_policy_against_catalog(body, cat)
    save_policy_to_db(body)
    r.set("config:recording_policy", policy_to_redis_json(body))
    r.publish(
        "config:updates",
        json.dumps({"type": "recording_policy", "payload": body.model_dump()}),
    )
    return {"ok": True, "policy": body.model_dump()}


@app.get("/api/v1/detections/latest")
def detections_latest() -> Any:
    raw = r.get("detections:latest")
    if not raw:
        return {}
    return json.loads(raw)


@app.get("/api/v1/telemetry")
def telemetry() -> Any:
    raw = r.get("telemetry:latest")
    if not raw:
        return {}
    return json.loads(raw)


@app.patch("/api/v1/model")
def patch_model(body: ModelPatch) -> dict[str, Any]:
    cur = ModelConfig()
    raw = r.get("config:model")
    if raw:
        cur = ModelConfig.model_validate_json(raw)
    data = cur.model_dump()
    if body.confidence_threshold is not None:
        data["confidence_threshold"] = body.confidence_threshold
    if body.iou_threshold is not None:
        data["iou_threshold"] = body.iou_threshold
    new = ModelConfig.model_validate(data)
    r.set("config:model", new.model_dump_json())
    r.publish(
        "config:updates",
        json.dumps({"type": "model", "payload": new.model_dump()}),
    )
    return {"ok": True, "model": new.model_dump()}


@app.patch("/api/v1/source")
def patch_source(body: SourcePatch) -> dict[str, Any]:
    src = SourceConfig(uri=body.uri, label=body.label or "custom")
    r.set("config:source", json.dumps({"uri": src.uri, "label": src.label}))
    r.publish("config:updates", json.dumps({"type": "source", "payload": src.model_dump()}))
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
        except redis.RedisError:
            return {"events": [], "source": "redis"}
        out = []
        for eid, fields in rows:
            out.append({"id": eid, **fields})
        return {"events": out, "source": "redis"}

    if not _db_ok:
        try:
            rows = r.xrevrange("events:detections", count=count)
        except redis.RedisError:
            return {"events": [], "source": "redis_fallback"}
        out = []
        for eid, fields in rows:
            out.append({"id": eid, **fields})
        return {"events": out, "source": "redis_fallback", "note": "postgres unavailable"}

    events = list_events(limit=count, offset=offset, label=label.lower() if label else None)
    return {"events": events, "source": "database"}


@app.get("/api/v1/snapshots/{name}")
def get_snapshot(name: str) -> FileResponse:
    base = Path(SNAPSHOT_DIR).resolve()
    path = (base / name).resolve()
    if not str(path).startswith(str(base)) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@app.websocket("/ws/telemetry")
async def ws_telemetry(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            raw = r.get("telemetry:latest")
            det = r.get("detections:latest")
            await ws.send_json(
                {
                    "telemetry": json.loads(raw) if raw else {},
                    "detections": json.loads(det) if det else {},
                },
            )
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        return


@app.get("/")
async def index() -> FileResponse:
    p = STATIC_DIR / "index.html"
    if not p.is_file():
        raise HTTPException(404, "static UI missing")
    return FileResponse(p)


app.mount("/assets", StaticFiles(directory=str(STATIC_DIR)), name="assets")
