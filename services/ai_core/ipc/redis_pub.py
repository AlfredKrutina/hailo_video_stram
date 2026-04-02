"""Redis: latest detections, telemetry, heartbeat, events stream, config pub/sub."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import redis

from shared.schemas.config import ModelConfig, SourceConfig
from shared.schemas.detections import DetectionFrame
from shared.schemas.recording import RecordingPolicy
from shared.schemas.telemetry import PipelineState, TelemetrySnapshot

logger = logging.getLogger("ai_core.redis")


class RedisPublisher:
    def __init__(self, url: str, heartbeat_ttl_s: int = 10) -> None:
        self._r = redis.from_url(url, decode_responses=True)
        self._heartbeat_ttl_s = heartbeat_ttl_s
        self._url = url
        # Sjednocuje poll + pub/sub source: bez toho by PATCH přes WS spustil rebuild dvakrát (pub/sub + poll).
        self._last_source_uri: str = ""

    def seed_source_uri(self, uri: str) -> None:
        """Nastav před startem subscribe/listen — shoda s YAML/SOURCE_URI zabrání falešnému prvnímu poll."""
        self._last_source_uri = (uri or "").strip()

    def publish_detections(self, frame: DetectionFrame) -> None:
        payload = frame.model_dump_json()
        self._r.set("detections:latest", payload)

    def publish_telemetry(self, snap: TelemetrySnapshot) -> None:
        self._r.set("telemetry:latest", snap.model_dump_json())

    def heartbeat(self) -> None:
        self._r.setex("ai:heartbeat", self._heartbeat_ttl_s, str(time.time()))

    def publish_event_person(self, snapshot_path: str, frame_id: int) -> None:
        try:
            self._r.xadd(
                "events:detections",
                {"kind": "person", "snapshot": snapshot_path, "frame_id": str(frame_id)},
                maxlen=500,
                approximate=True,
            )
        except redis.RedisError as e:
            logger.warning("redis_xadd_failed", extra={"extra_data": {"err": str(e)}})

    def publish_detection_stream(
        self,
        *,
        db_id: str,
        label: str,
        frame_id: int,
        snapshot: str,
        attrs_json: str,
    ) -> None:
        try:
            self._r.xadd(
                "events:detections",
                {
                    "db_id": db_id,
                    "kind": label,
                    "frame_id": str(frame_id),
                    "snapshot": snapshot,
                    "attributes": attrs_json,
                },
                maxlen=500,
                approximate=True,
            )
        except redis.RedisError as e:
            logger.warning("redis_xadd_failed", extra={"extra_data": {"err": str(e)}})

    def subscribe_config(
        self,
        on_model: Any,
        on_recording_policy: Any | None = None,
        on_source: Any | None = None,
    ) -> None:
        ps = redis.from_url(self._url, decode_responses=True).pubsub(ignore_subscribe_messages=True)
        ps.subscribe("config:updates")

        def loop() -> None:
            for msg in ps.listen():
                if msg["type"] != "message":
                    continue
                try:
                    data = json.loads(msg["data"])
                    t = data.get("type")
                    if t == "model":
                        on_model(ModelConfig.model_validate(data.get("payload", {})))
                    elif t == "recording_policy" and on_recording_policy:
                        on_recording_policy(
                            RecordingPolicy.model_validate(data.get("payload", {})),
                        )
                    elif t == "source" and on_source:
                        src = SourceConfig.model_validate(data.get("payload", {}))
                        u = (src.uri or "").strip()
                        if u and u != self._last_source_uri:
                            self._last_source_uri = u
                            on_source(u)
                except Exception as e:
                    logger.warning("config_msg_failed", extra={"extra_data": {"err": str(e)}})

        t = threading.Thread(target=loop, daemon=True)
        t.start()

    def get_recording_policy_json(self) -> str | None:
        return self._r.get("config:recording_policy")

    def listen_source_changes(self, on_uri: Any) -> None:
        """Poll config:source for hot-swap when set by API (backup if pub/sub missed a message)."""

        def loop() -> None:
            while True:
                try:
                    raw = self._r.get("config:source")
                    if raw:
                        payload = json.loads(raw)
                        uri = (payload.get("uri") or "").strip()
                        if uri and uri != self._last_source_uri:
                            self._last_source_uri = uri
                            on_uri(uri)
                except Exception as e:
                    logger.debug("source_poll", extra={"extra_data": {"err": str(e)}})
                time.sleep(0.5)

        threading.Thread(target=loop, daemon=True).start()


def save_snapshot_jpeg(snapshot_dir: str, frame_id: int, jpeg_bytes: bytes) -> str | None:
    try:
        d = Path(snapshot_dir)
        d.mkdir(parents=True, exist_ok=True)
        name = f"snap_{frame_id}_{int(time.time() * 1000)}.jpg"
        p = d / name
        p.write_bytes(jpeg_bytes)
        return str(p)
    except OSError as e:
        logger.error("snapshot_write_failed", extra={"extra_data": {"err": str(e)}})
        return None
