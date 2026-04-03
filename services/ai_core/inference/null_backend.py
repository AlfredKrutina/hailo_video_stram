"""Backend bez CPU inferenc — detekce dodává výhradně GStreamer (hailonet / hailofilter)."""

from __future__ import annotations

import time

import numpy as np

from shared.schemas.config import ModelConfig
from shared.schemas.detections import DetectionFrame


class NullInferenceBackend:
    """`infer()` je no-op; Redis plní gst_pipeline + hailo callbacky."""

    def infer(
        self,
        rgb: np.ndarray,
        frame_id: int,
        timestamp_ns: int,
        source_uri: str,
        model: ModelConfig,
    ) -> DetectionFrame:
        h, w = rgb.shape[:2]
        return DetectionFrame(
            frame_id=frame_id,
            timestamp_ns=timestamp_ns,
            width=w,
            height=h,
            source_uri=source_uri,
            detections=[],
        )
