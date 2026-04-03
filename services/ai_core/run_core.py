"""
ai_core: single process that ties together video ingest, inference, IPC, and optional DB writes.

Threads / asyncio:
- Main thread: GstVisionPipeline GLib loop or dummy loop; blocking `run()` tail.
- asyncio: video WebSocket server (JPEG frames, dedicated thread running asyncio.run).
- Threads: Redis config subscriber, source poll, telemetry publisher, DB writer consumer.

Failure modes:
- GStreamer errors trigger recovery (see pipeline.gst_pipeline); telemetry exposes `last_error`.
- DB disabled if `init_db()` fails or `DATABASE_URL` unset; events dropped with log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import signal
import threading
import time
from typing import Any

import numpy as np

from shared.logging_setup import setup_logging
from shared.recording_eval import EventRateLimiter, build_stored_attributes, should_persist_detection
from shared.schemas.config import AppConfig, ModelConfig
from shared.schemas.detections import DetectionFrame
from shared.schemas.recording import RecordingPolicy, default_policy
from shared.schemas.telemetry import PipelineState, TelemetrySnapshot

from services.ai_core.config.load import load_app_config
from services.ai_core.inference.factory import create_inference_backend
from services.ai_core.ipc.redis_pub import RedisPublisher, save_snapshot_jpeg
from services.ai_core.video_ws_server import run_video_ws_server
from services.ai_core.pipeline.state import PipelineController
from services.ai_core.sensors import read_hailo_temp_c, read_soc_temp_c
from services.persistence.recording_store import insert_detection_event
from services.persistence.session import get_database_url, init_db

logger = logging.getLogger("ai_core")


class CoreApp:
    """
    Loads config from YAML + env, wires Redis, optional Postgres, Hailo stub/real, and GStreamer.
    Hot-swap source URI via Redis key `config:source` (set by web PATCH /api/v1/source).
    """

    def __init__(self) -> None:
        self.cfg: AppConfig = load_app_config()
        setup_logging("ai_core")
        self._controller = PipelineController()
        self._redis = RedisPublisher(self.cfg.redis_url)
        self._backend, self._infer_probe = create_inference_backend(self.cfg)
        self._last_jpeg: bytes | None = None
        self._pipeline_state: PipelineState = PipelineState.IDLE
        self._last_error: str | None = None
        self._gst: Any = None
        self._jpeg_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=4)
        self._stop = threading.Event()
        self._hailo_oopd_attempts = 0
        self._hailo_recovery_lock = threading.Lock()

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

    @staticmethod
    def _is_hailo_rt_like_exception(exc: BaseException) -> bool:
        if "HailoRT" in type(exc).__name__:
            return True
        s = str(exc).upper()
        return "OUT_OF_PHYSICAL" in s or "HAILO_OUT_OF_PHYSICAL" in s or "HAILO_OUT_OF_PHYSICAL_DEVICES" in s

    def _publish_detections_from_gst(self, frame: DetectionFrame) -> None:
        """Hailo GStreamer větev — detekce už v frame, bez CPU infer."""
        try:
            self._apply_detection_side_effects(frame, frame.source_uri)
        except BaseException as e:
            probe = self._infer_probe if isinstance(self._infer_probe, dict) else {}
            if probe.get("infer_backend_active") == "hailo_gst" and self._is_hailo_rt_like_exception(e):
                logger.error("hailo_rt_exc_in_gst_publish", extra={"extra_data": {"err": str(e)}})
                msg = str(e)[:1200]
                threading.Thread(
                    target=lambda m=msg: self._handle_hailo_resource_error(m),
                    daemon=True,
                    name="hailo-oopd-from-publish",
                ).start()
                return
            raise

    def _handle_hailo_resource_error(self, detail: str) -> None:
        """Po Hailo OOPD: stop → release → 2 s → start; max 3 pokusy, pak ONNX + source_error."""
        with self._hailo_recovery_lock:
            self._hailo_oopd_attempts += 1
            attempt = self._hailo_oopd_attempts
            if os.environ.get("ENVIRONMENT", "").lower() == "staging":
                logger.info(
                    "hailo_oopd_recovery",
                    extra={"extra_data": {"attempt": attempt, "detail": detail[:400]}},
                )
            if attempt > 3:
                self._fallback_to_onnx_after_hailo_oopd(detail)
                return
            if self._gst:
                self._gst.stop()
            from services.ai_core.pipeline.hailo_device_release import release_hailo_device

            release_hailo_device(self._redis, detail)
            time.sleep(2.0)
            if self._gst:
                self._gst.start()

    def _fallback_to_onnx_after_hailo_oopd(self, reason: str) -> None:
        onnx_path = os.environ.get("RPY_ONNX_MODEL_PATH", "").strip()
        if not onnx_path:
            self._redis.publish_source_error_event(
                "Hailo nedostupné po opakovaných pokusech; ONNX fallback nelze — nastavte RPY_ONNX_MODEL_PATH.",
                configured_uri=self.cfg.source.uri,
            )
            return
        os.environ["RPY_INFER_BACKEND"] = "onnx"
        self._backend, self._infer_probe = create_inference_backend(self.cfg)
        if self._gst:
            self._gst.stop()
        from services.ai_core.pipeline.gst_pipeline import (  # noqa: PLC0415
            GstVisionPipeline,
            _GST_AVAILABLE,
            gst_init,
        )

        if not (_GST_AVAILABLE and gst_init()):
            logger.warning("gst_unavailable_after_onnx_fallback")
            return
        self._gst = GstVisionPipeline(
            self.cfg,
            self._controller,
            self._on_frame,
            self._on_jpeg,
            self._on_state,
            redis_publisher=self._redis,
            hailo_gst_mode=False,
            publish_detections_gst=self._publish_detections_from_gst,
            on_hailo_resource_error=None,
        )
        self._gst.start()
        self._hailo_oopd_attempts = 0
        self._redis.publish_source_error_event(
            f"Hailo OOPD — přepnuto na ONNX: {reason[:800]}",
            configured_uri=self.cfg.source.uri,
        )

    def _apply_detection_side_effects(self, frame: DetectionFrame, source_uri: str) -> None:
        self._redis.publish_detections(frame)
        with self._policy_lock:
            policy = self._recording_policy
        snapshot_path: str | None = None
        if policy.store_snapshots and self._last_jpeg:
            for det in frame.detections:
                if should_persist_detection(det, policy):
                    snapshot_path = save_snapshot_jpeg(
                        self.cfg.snapshot_dir,
                        frame.frame_id,
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
                        "frame_id": frame.frame_id,
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

    def _on_frame(
        self,
        rgb: np.ndarray,
        frame_id: int,
        ts_ns: int,
        source_uri: str,
        model: ModelConfig,
    ) -> None:
        if isinstance(self._infer_probe, dict) and self._infer_probe.get("infer_backend_active") == "hailo_gst":
            return
        frame = self._backend.infer(rgb, frame_id, ts_ns, source_uri, model)
        self._apply_detection_side_effects(frame, source_uri)

    def _telemetry_loop(self) -> None:
        while not self._stop.is_set():
            fps = None
            lat = None
            extra: dict[str, Any] = {}
            if self._gst is not None:
                try:
                    fps = self._gst.get_fps()
                    lat = self._gst.get_latency_ms()
                except Exception as e:
                    extra["telemetry_metrics_err"] = str(e)
            if self._gst and hasattr(self._gst, "get_diagnostics"):
                try:
                    diag = self._gst.get_diagnostics()
                    extra.update(diag)
                    sid = diag.get("source_idle_message")
                    if sid:
                        extra["source_error"] = sid
                except Exception as e:
                    extra["diagnostics_err"] = str(e)
            probe = getattr(self, "_infer_probe", None)
            if isinstance(probe, dict):
                extra["infer_backend_active"] = probe.get("infer_backend_active")
                extra["hailo_device_present"] = probe.get("hailo_device_present")
                if probe.get("hailo_device_path"):
                    extra["hailo_device_path"] = probe.get("hailo_device_path")
                if probe.get("infer_backend_note"):
                    extra["infer_backend_note"] = probe.get("infer_backend_note")
                if probe.get("onnx_model_path"):
                    extra["onnx_model_path"] = probe.get("onnx_model_path")
                if "hailo_infer_implemented" in probe:
                    extra["hailo_infer_implemented"] = probe.get("hailo_infer_implemented")
                if probe.get("hailo_gst_stack") is not None:
                    extra["hailo_gst_stack"] = probe.get("hailo_gst_stack")
            tex = getattr(self._backend, "telemetry_extra", None)
            if callable(tex):
                try:
                    extra.update(tex())
                except Exception as e:
                    extra["telemetry_extra_err"] = str(e)[:200]
            snap = TelemetrySnapshot(
                pipeline_state=self._pipeline_state,
                inference_latency_ms=lat,
                fps=fps,
                soc_temp_c=read_soc_temp_c(),
                hailo_temp_c=read_hailo_temp_c(),
                camera_connected=self._pipeline_state
                in (
                    PipelineState.RUNNING,
                    PipelineState.RECOVERING,
                    PipelineState.RECONFIGURING,
                ),
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
        self._hailo_oopd_attempts = 0
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

        self._redis.seed_source_uri(self.cfg.source.uri)
        self._redis.subscribe_config(
            self._apply_model,
            self._apply_recording_policy,
            on_source=self._apply_source,
        )
        self._redis.listen_source_changes(self._apply_source)

        threading.Thread(target=self._telemetry_loop, daemon=True).start()
        threading.Thread(target=self._writer_loop, daemon=True).start()

        async def amain() -> None:
            await run_video_ws_server("0.0.0.0", self.cfg.mjpeg_port, self._jpeg_queue)

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
            hailo_gst = (
                isinstance(self._infer_probe, dict)
                and self._infer_probe.get("infer_backend_active") == "hailo_gst"
            )
            self._gst = GstVisionPipeline(
                self.cfg,
                self._controller,
                self._on_frame,
                self._on_jpeg,
                self._on_state,
                redis_publisher=self._redis,
                hailo_gst_mode=hailo_gst,
                publish_detections_gst=self._publish_detections_from_gst,
                on_hailo_resource_error=self._handle_hailo_resource_error if hailo_gst else None,
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
