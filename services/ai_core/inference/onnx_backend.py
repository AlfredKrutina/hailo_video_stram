"""CPU inference přes ONNX Runtime — očekává YOLOv8 ONNX export (výstup [1, 4+nc, anchors])."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import numpy as np

from shared.schemas.config import ModelConfig
from shared.schemas.detections import DetectionFrame

from services.ai_core.inference.yolo_postprocess import postprocess_yolov8_head

logger = logging.getLogger("ai_core.inference.onnx")


class OnnxCpuBackend:
    """YOLOv8n-style ONNX: vstup NCHW float32 0..1, výstup [1, 4+nc, N]."""

    def __init__(self, model_path: str) -> None:
        p = Path(model_path)
        if not p.is_file():
            raise FileNotFoundError(f"ONNX model not found: {model_path}")
        import onnxruntime as ort  # noqa: PLC0415

        try:
            import cv2  # noqa: PLC0415
        except ImportError as e:
            raise RuntimeError(
                "opencv-python-headless required for ONNX backend (pip install opencv-python-headless)",
            ) from e

        self._cv2 = cv2
        self._session = ort.InferenceSession(
            str(p),
            providers=["CPUExecutionProvider"],
        )
        self._inp = self._session.get_inputs()[0]
        self._name = self._inp.name
        shape = self._inp.shape
        if len(shape) != 4:
            raise ValueError(f"expected NCHW input, got shape {shape}")

        def _dim(v: Any, default: int) -> int:
            if v is None or (isinstance(v, str) and not v.isdigit()):
                return default
            try:
                i = int(v)
                return i if i > 0 else default
            except (TypeError, ValueError):
                return default

        self._in_h = _dim(shape[2], 640)
        self._in_w = _dim(shape[3], 640)
        out0 = self._session.get_outputs()[0]
        self._out_shape = tuple(out0.shape)
        logger.info(
            "onnx_backend_ready",
            extra={"extra_data": {"path": str(p), "in_hw": (self._in_h, self._in_w), "out": self._out_shape}},
        )

    def infer(
        self,
        rgb: np.ndarray,
        frame_id: int,
        timestamp_ns: int,
        source_uri: str,
        model: ModelConfig,
    ) -> DetectionFrame:
        h, w = rgb.shape[:2]
        t0 = time.perf_counter()
        img = self._cv2.resize(rgb, (self._in_w, self._in_h), interpolation=self._cv2.INTER_LINEAR)
        chw = np.transpose(img, (2, 0, 1)).astype(np.float32) / 255.0
        batch = np.expand_dims(chw, 0)
        out = self._session.run(None, {self._name: batch})[0]
        detections = postprocess_yolov8_head(out, w, h, model, self._in_w, self._in_h)
        _ = (time.perf_counter() - t0) * 1000
        return DetectionFrame(
            frame_id=frame_id,
            timestamp_ns=timestamp_ns,
            width=w,
            height=h,
            source_uri=source_uri,
            detections=detections,
        )
