"""Inference backend: Hailo when available, else deterministic stub for dev."""

from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING, Protocol

import numpy as np

from shared.schemas.config import ModelConfig
from shared.schemas.detections import BoundingBox, Detection, DetectionFrame

if TYPE_CHECKING:
    pass

logger = logging.getLogger("ai_core.inference")


class InferenceBackend(Protocol):
    def infer(
        self,
        rgb: np.ndarray,
        frame_id: int,
        timestamp_ns: int,
        source_uri: str,
        model: ModelConfig,
    ) -> DetectionFrame: ...


class StubHailoBackend:
    """CPU-light fake detections for CI and systems without /dev/hailo0."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def infer(
        self,
        rgb: np.ndarray,
        frame_id: int,
        timestamp_ns: int,
        source_uri: str,
        model: ModelConfig,
    ) -> DetectionFrame:
        h, w = rgb.shape[:2]
        t = time.monotonic() - self._t0
        cx = 0.5 + 0.2 * math.sin(t * 0.7)
        cy = 0.5 + 0.15 * math.cos(t * 0.9)
        bw, bh = 0.12, 0.22
        conf = 0.55 + 0.1 * math.sin(t * 2.0)
        conf = max(0.05, min(0.95, conf))
        detections: list[Detection] = []
        if conf >= model.confidence_threshold:
            detections.append(
                Detection(
                    class_id=0,
                    label="person",
                    confidence=conf,
                    box=BoundingBox(
                        x=max(0.0, min(1.0 - bw, cx - bw / 2)),
                        y=max(0.0, min(1.0 - bh, cy - bh / 2)),
                        w=bw,
                        h=bh,
                    ),
                    attributes={
                        "person_upper_color": "#64748b",
                        "person_lower_color": "#334155",
                        "person_outer_color": "#1e293b",
                    },
                ),
            )
        return DetectionFrame(
            frame_id=frame_id,
            timestamp_ns=timestamp_ns,
            width=w,
            height=h,
            source_uri=source_uri,
            detections=detections,
        )


def try_create_hailo_backend(use_hailo: bool) -> InferenceBackend:
    if not use_hailo:
        logger.info("hailo_disabled_using_stub")
        return StubHailoBackend()
    logger.info("legacy_use_hailo_stub_infer_in_gstreamer")
    return StubHailoBackend()
