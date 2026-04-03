"""Výběr inference backendu: stub, Hailo, ONNX CPU (env + config)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from shared.schemas.config import AppConfig

from services.ai_core.inference.hailo_backend import (
    InferenceBackend,
    StubHailoBackend,
    try_create_hailo_backend,
)

logger = logging.getLogger("ai_core.inference")


def _hailo_device_path() -> Path:
    return Path(os.environ.get("RPY_HAILO_DEVICE", "/dev/hailo0").strip() or "/dev/hailo0")


def _hailo_device_present() -> bool:
    return _hailo_device_path().exists()


def create_inference_backend(cfg: AppConfig) -> tuple[InferenceBackend, dict[str, Any]]:
    """
    Vrací (backend, statický_probe) pro telemetrii.

    Env:
    - RPY_INFER_BACKEND: stub | hailo | onnx — výběr výslovný; prázdné = legacy chování přes use_hailo.
    - RPY_ONNX_MODEL_PATH: cesta k .onnx (YOLOv8 export [1,84,N]).
    """
    mode = os.environ.get("RPY_INFER_BACKEND", "").strip().lower()
    probe: dict[str, Any] = {
        "infer_backend_requested": mode or "(default)",
        "hailo_device_present": _hailo_device_present(),
        "hailo_device_path": str(_hailo_device_path()),
        "rpy_onnx_model_path_set": bool(os.environ.get("RPY_ONNX_MODEL_PATH", "").strip()),
        "hailo_infer_implemented": False,
    }

    if mode == "stub":
        probe["infer_backend_active"] = "stub"
        logger.info("infer_backend_forced_stub")
        return StubHailoBackend(), probe

    if mode == "onnx":
        path = os.environ.get("RPY_ONNX_MODEL_PATH", "").strip()
        if not path:
            logger.warning("infer_onnx_no_model_path_stub")
            probe["infer_backend_active"] = "stub"
            probe["infer_backend_note"] = "RPY_INFER_BACKEND=onnx but RPY_ONNX_MODEL_PATH missing"
            return StubHailoBackend(), probe
        try:
            from services.ai_core.inference.onnx_backend import OnnxCpuBackend  # noqa: PLC0415

            b = OnnxCpuBackend(path)
            probe["infer_backend_active"] = "onnx"
            probe["onnx_model_path"] = path
            logger.info("infer_backend_onnx", extra={"extra_data": {"path": path}})
            return b, probe
        except Exception as e:
            logger.warning("infer_onnx_failed_stub", extra={"extra_data": {"err": str(e)}})
            probe["infer_backend_active"] = "stub"
            probe["infer_backend_note"] = f"onnx init failed: {e}"
            return StubHailoBackend(), probe

    if mode == "hailo":
        try:
            from services.ai_core.inference.hailo_real import HailoBackend  # noqa: PLC0415

            b = HailoBackend()
            probe["infer_backend_active"] = "hailo"
            probe["hailo_infer_implemented"] = getattr(b, "infer_implemented", True)
            return b, probe
        except Exception as e:
            logger.warning("infer_hailo_failed_stub", extra={"extra_data": {"err": str(e)}})
            probe["infer_backend_active"] = "stub"
            probe["infer_backend_note"] = str(e)[:300]
            return StubHailoBackend(), probe

    # Legacy: USE_HAILO / cfg.use_hailo
    backend = try_create_hailo_backend(cfg.use_hailo)
    if isinstance(backend, StubHailoBackend):
        probe["infer_backend_active"] = "stub"
        probe["infer_backend_note"] = "legacy try_create_hailo_backend → stub"
    else:
        probe["infer_backend_active"] = "hailo"
        probe["hailo_infer_implemented"] = getattr(backend, "infer_implemented", True)
    return backend, probe
