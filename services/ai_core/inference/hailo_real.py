"""
Optional Hailo-accelerated path. Placeholder: integrate hailo_platform / HailoRT
per your device image. Raises ImportError if not configured so stub is used.
"""

from __future__ import annotations

import logging

import numpy as np

from shared.schemas.config import ModelConfig
from shared.schemas.detections import DetectionFrame

logger = logging.getLogger("ai_core.inference.hailo_real")


class HailoBackend:
    def __init__(self) -> None:
        # When integrating: load hef, create device, configure network.
        raise RuntimeError(
            "HailoBackend not wired: add hailo_platform and model paths for your Pi image.",
        )

    def infer(
        self,
        rgb: np.ndarray,
        frame_id: int,
        timestamp_ns: int,
        source_uri: str,
        model: ModelConfig,
    ) -> DetectionFrame:
        _ = rgb, frame_id, timestamp_ns, source_uri, model
        raise NotImplementedError
