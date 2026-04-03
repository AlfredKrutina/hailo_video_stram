"""
Hailo-8 integrační bod: ověří zařízení, HEF a volitelně hailo_platform.

Plná inference (HEF → tensor → NMS) závisí na verzi HailoRT / DFC u cílového image —
doplňte `infer()` podle dokumentace vašeho SDK. Dokud není napojeno, vrací prázdné detekce
a loguje varování (pipeline a MJPEG běží).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

from shared.schemas.config import ModelConfig
from shared.schemas.detections import DetectionFrame

logger = logging.getLogger("ai_core.inference.hailo_real")

_warned_infer = False


class HailoBackend:
    def __init__(self) -> None:
        if not Path("/dev/hailo0").exists():
            raise RuntimeError(
                "Hailo: chybí /dev/hailo0 — namapujte zařízení v docker-compose (devices) a ověřte driver na hostu.",
            )
        hef = os.environ.get("RPY_HAILO_HEF_PATH", "").strip()
        if not hef:
            raise RuntimeError(
                "Hailo: nastavte RPY_HAILO_HEF_PATH na .hef model (nebo použijte RPY_INFER_BACKEND=onnx / stub).",
            )
        if not Path(hef).is_file():
            raise RuntimeError(f"Hailo: RPY_HAILO_HEF_PATH neexistuje: {hef}")
        self._hef_path = hef
        try:
            import hailo_platform  # noqa: F401, PLC0415

            self._hailo_pkg = True
        except ImportError as e:
            raise RuntimeError(
                "Hailo: nainstalujte hailo_platform / HailoRT wheel odpovídající vašemu image (pip v Dockerfile).",
            ) from e
        logger.info(
            "hailo_backend_init_ok",
            extra={"extra_data": {"hef": hef, "hailo_platform": self._hailo_pkg}},
        )

    def infer(
        self,
        rgb: np.ndarray,
        frame_id: int,
        timestamp_ns: int,
        source_uri: str,
        model: ModelConfig,
    ) -> DetectionFrame:
        global _warned_infer
        h, w = rgb.shape[:2]
        if not _warned_infer:
            _warned_infer = True
            logger.warning(
                "hailo_infer_stub_empty",
                extra={
                    "extra_data": {
                        "msg": "Doplňte HailoBackend.infer() podle HailoRT — zatím prázdné detekce.",
                        "hef": self._hef_path,
                    },
                },
            )
        _ = model
        return DetectionFrame(
            frame_id=frame_id,
            timestamp_ns=timestamp_ns,
            width=w,
            height=h,
            source_uri=source_uri,
            detections=[],
        )
