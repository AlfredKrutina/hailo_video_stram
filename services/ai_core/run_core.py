"""Orchestrates GStreamer (or dummy ingest), inference, Redis, MJPEG, DB events."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import signal
import threading
import time
from typing import Any

import numpy as np

from shared.logging_setup import setup_logging
from shared.recording_eval import EventRateLimiter, build_stored_attributes, should_persist_detection
from shared.schemas.config import AppConfig, ModelConfig
from shared.schemas.recording import RecordingPolicy, default_policy
from shared.schemas.telemetry import PipelineState, TelemetrySnapshot

from services.ai_core.config.load import load_app_config
from services.ai_core.inference.hailo_backend import try_create_hailo_backend
from services.ai_core.ipc.redis_pub import RedisPublisher, save_snapshot_jpeg
from services.ai_core.mjpeg_server import run_mjpeg_server
from services.ai_core.pipeline.state import PipelineController
from services.ai_core.sensors import read_hailo_temp_c, read_soc_temp_c
from services.persistence.recording_store import insert_detection_event
from services.persistence.session import get_database_url, init_db

logger = logging.getLogger("ai_core")


class CoreApp:
    def __init__(self) -> None:
        self.cfg: AppConfig = load_app_config()
        setup_logging("ai_core")
        self._controller = PipelineController()
        self._redis = RedisPublisher(self.cfg.redis_url)
        self._backend = try_create_hailo_backend(self.cfg.use_hailo)
        self._last_jpeg: bytes | None = None
        self._pipeline_state: PipelineState = PipelineState.IDLE
        self._last_error: str | None = None
        self._gst: Any = None
        self._jpeg_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=4)
        self._stop = threading.Event()

        self._policy_lock = threading.Lock()
        self._recording_policy: RecordingPolicy = default_policy()
        self._rate = EventRateLimiter(self._recording_policy.max_events_per_minute)
        self._event_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=512)
        self._db_enabled = bool(get_database_url())
        if self._db_enabled:
            if not init_db():
                logger.warning("db_init_failed_disabling_persistence")
                self._db_enabled = False
        self._load_policy_from_redis()

    def _load_policy_from_redis(self) -> None:
        raw = self._redis.get_recording_policy_json()
        if not raw:
            return
        try:
            with self._policy_lock:
                self._recording_policy = RecordingPolicy.model_validate_json(raw)
                self._rate.set_max(self._recording_policy.max_events_per_minute)
        except Exception as e:
            logger.warning("policy_load_failed", extra={"extra_data": {"err": str(e)}})

    def _apply_recording_policy(self, p: RecordingPolicy) -> None:
        with self._policy_lock:
            self._recording_policy = p
            self._rate.set_max(p.max_events_per_minute)

    def _writer_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._event_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not self._db_enabled:
                continue
            try:
                eid = insert_detection_event(**item)
                if eid is not None:
                    self._redis.publish_detection_stream(
                        db_id=str(eid),
                        label=item["label"],
                        frame_id=item["frame_id"],
                        snapshot=item.get("snapshot_path") or "",
                        attrs_json=json.dumps(item.get("attributes") or {}),
                    )
            except Exception as e:
                logger.error("db_insert_failed", extra={"extra_data": {"err": str(e)}})

    def _on_state(self, state: PipelineState, err: str | None) -> None:
        self._pipeline_state = state
        self._last_error = err

    def _on_jpeg(self, data: bytes) -> None:
        self._last_jpeg = data
        try:
            self._jpeg_queue.put_nowait(data)
        except queue.Full:
            try:
                self._jpeg_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._jpeg_queue.put_nowait(data)
            except queue.Full:
                pass

    def _on_frame(
        self,
        rgb: np.ndarray,
        frame_id: int,
        ts_ns: int,
        source_uri: str,
        model: ModelConfig,
    ) -> None:
        frame = self._backend.infer(rgb, frame_id, ts_ns, source_uri, model)
        self._redis.publish_detections(frame)

        with self._policy_lock:
            policy = self._recording_policy

        snapshot_path: str | None = None
        if policy.store_snapshots and self._last_jpeg:
            for det in frame.detections:
                if should_persist_detection(det, policy):
                    snapshot_path = save_snapshot_jpeg(
                        self.cfg.snapshot_dir,
                        frame_id,
                        self._last_jpeg,
                    )
                    break

        for det in frame.detections:
            if not should_persist_detection(det, policy):
                continue
            if not self._rate.allow():
                logger.debug("event_rate_limited")
                continue
            attrs = build_stored_attributes(det, policy)
            try:
                self._event_queue.put_nowait(
                    {
                        "frame_id": frame_id,
                        "source_uri": source_uri,
                        "label": det.label,
                        "class_id": det.class_id,
                        "confidence": float(det.confidence),
                        "snapshot_path": snapshot_path if policy.store_snapshots else None,
                        "attributes": attrs,
                    },
                )
            except queue.Full:
                logger.warning("event_queue_full_drop")

    def _telemetry_loop(self) -> None:
        while not self._stop.is_set():
            fps = None
            lat = None
            extra: dict[str, Any] = {}
            if self._gst and hasattr(self._gst, "get_diagnostics"):
                try:
                    extra.update(self._gst.get_diagnostics())
                except Exception as e:
                    extra["diagnostics_err"] = str(e)
            snap = TelemetrySnapshot(
                pipeline_state=self._pipeline_state,
                inference_latency_ms=lat,
                fps=fps,
                soc_temp_c=read_soc_temp_c(),
                hailo_temp_c=read_hailo_temp_c(),
                camera_connected=self._pipeline_state == PipelineState.RUNNING,
                last_error=self._last_error,
                extra=extra,
            )
            self._redis.publish_telemetry(snap)
            self._redis.heartbeat()
            time.sleep(0.5)

    def _apply_model(self, m: ModelConfig) -> None:
        self.cfg.model = m
        if self._gst:
            self._gst.apply_model_config(m)

    def _apply_source(self, uri: str) -> None:
        self.cfg.source.uri = uri
        if self._gst:
            self._gst.apply_source_uri(uri)

    def run(self) -> None:
        def handle_sig(*_a: object) -> None:
            self._stop.set()
            if self._gst:
                self._gst.stop()

        signal.signal(signal.SIGINT, handle_sig)
        signal.signal(signal.SIGTERM, handle_sig)

        self._redis.subscribe_config(self._apply_model, self._apply_recording_policy)
        self._redis.listen_source_changes(self._apply_source)

        threading.Thread(target=self._telemetry_loop, daemon=True).start()
        threading.Thread(target=self._writer_loop, daemon=True).start()

        async def amain() -> None:
            await run_mjpeg_server("0.0.0.0", self.cfg.mjpeg_port, self._jpeg_queue)

        def run_async() -> None:
            asyncio.run(amain())

        threading.Thread(target=run_async, daemon=True).start()
        time.sleep(0.3)

        from services.ai_core.pipeline.gst_pipeline import (  # noqa: PLC0415
            GstVisionPipeline,
            _GST_AVAILABLE,
            gst_init,
        )

        if _GST_AVAILABLE and gst_init():
            self._gst = GstVisionPipeline(
                self.cfg,
                self._controller,
                self._on_frame,
                self._on_jpeg,
                self._on_state,
            )
            ok = self._gst.start()
            if not ok:
                logger.warning("gst_start_failed_fallback_dummy")
                self._dummy_loop()
        else:
            logger.warning("gst_unavailable_using_dummy_ingest")
            self._dummy_loop()

        while not self._stop.is_set():
            time.sleep(0.5)

    def _dummy_loop(self) -> None:
        self._controller.force(PipelineState.RUNNING)
        self._on_state(PipelineState.RUNNING, None)
        fid = 0
        while not self._stop.is_set():
            fid += 1
            rgb = np.zeros((480, 640, 3), dtype=np.uint8)
            rgb[:, :] = (40, 40, 48)
            self._on_frame(rgb, fid, time.time_ns(), self.cfg.source.uri, self.cfg.model)
            time.sleep(0.1)
        self._on_state(PipelineState.IDLE, None)


def main() -> None:
    CoreApp().run()


if __name__ == "__main__":
    main()
