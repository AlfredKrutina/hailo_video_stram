"""CPU inference přes ONNX Runtime — očekává YOLOv8 ONNX export (výstup [1, 4+nc, anchors])."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import numpy as np

from shared.schemas.config import ModelConfig
from shared.schemas.detections import BoundingBox, Detection, DetectionFrame

logger = logging.getLogger("ai_core.inference.onnx")

# COCO 80 — index 0 = person
_COCO80 = (
    "person,bicycle,car,motorcycle,airplane,bus,train,truck,boat,traffic light,fire hydrant,stop sign,"
    "parking meter,bench,bird,cat,dog,horse,sheep,cow,elephant,bear,zebra,giraffe,backpack,umbrella,"
    "handbag,tie,suitcase,frisbee,skis,snowboard,sports ball,kite,baseball bat,baseball glove,skateboard,"
    "surfboard,tennis racket,bottle,wine glass,cup,fork,knife,spoon,bowl,banana,apple,sandwich,orange,"
    "broccoli,carrot,hot dog,pizza,donut,cake,chair,couch,potted plant,bed,dining table,toilet,tv,"
    "laptop,mouse,remote,keyboard,cell phone,microwave,oven,toaster,sink,refrigerator,book,clock,vase,"
    "scissors,teddy bear,hair drier,toothbrush"
).split(",")


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    ar = (a[2] - a[0]) * (a[3] - a[1])
    br = (b[2] - b[0]) * (b[3] - b[1])
    u = ar + br - inter
    return inter / u if u > 0 else 0.0


def _nms_xyxy(boxes: np.ndarray, scores: np.ndarray, iou_thr: float, max_det: int) -> list[int]:
    if len(boxes) == 0:
        return []
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0 and len(keep) < max_det:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        ious = np.array([_iou(boxes[i], boxes[j]) for j in rest])
        order = rest[ious < iou_thr]
    return keep


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
        detections = self._postprocess(out, w, h, model)
        _ = (time.perf_counter() - t0) * 1000
        return DetectionFrame(
            frame_id=frame_id,
            timestamp_ns=timestamp_ns,
            width=w,
            height=h,
            source_uri=source_uri,
            detections=detections,
        )

    def _postprocess(
        self,
        out: np.ndarray,
        orig_w: int,
        orig_h: int,
        model: ModelConfig,
    ) -> list[Detection]:
        # [1, 4+nc, N] or [1, N, 4+nc]
        if out.ndim != 3:
            return []
        _, d1, d2 = out.shape
        if min(d1, d2) < 6:
            return []
        if d1 < d2:
            # [1, 4+nc, N] — YOLOv8 ONNX default
            pred = out[0]
            nc = d1 - 4
            n = d2
            data = pred.T  # [N, 4+nc]
        else:
            # [1, N, 4+nc]
            data = out[0]
            nc = data.shape[1] - 4
            n = data.shape[0]
        if nc < 1:
            return []
        boxes_xyxy: list[np.ndarray] = []
        scores: list[float] = []
        classes: list[int] = []
        sx = orig_w / self._in_w
        sy = orig_h / self._in_h
        for i in range(n):
            row = data[i]
            cx, cy, bw, bh = float(row[0]), float(row[1]), float(row[2]), float(row[3])
            cls_scores = row[4 : 4 + nc]
            ci = int(np.argmax(cls_scores))
            conf = float(cls_scores[ci])
            if conf < model.confidence_threshold:
                continue
            x1 = (cx - bw / 2) * sx
            y1 = (cy - bh / 2) * sy
            x2 = (cx + bw / 2) * sx
            y2 = (cy + bh / 2) * sy
            x1 = max(0.0, min(orig_w - 1, x1))
            y1 = max(0.0, min(orig_h - 1, y1))
            x2 = max(0.0, min(orig_w, x2))
            y2 = max(0.0, min(orig_h, y2))
            if x2 <= x1 or y2 <= y1:
                continue
            boxes_xyxy.append(np.array([x1, y1, x2, y2], dtype=np.float32))
            scores.append(conf)
            classes.append(ci)
        if not boxes_xyxy:
            return []
        b_arr = np.stack(boxes_xyxy)
        s_arr = np.array(scores, dtype=np.float32)
        keep = _nms_xyxy(b_arr, s_arr, model.iou_threshold, max_det=50)
        out_dets: list[Detection] = []
        for k in keep:
            x1, y1, x2, y2 = b_arr[k]
            ci = classes[k]
            label = _COCO80[ci] if ci < len(_COCO80) else f"class_{ci}"
            out_dets.append(
                Detection(
                    class_id=ci,
                    label=label,
                    confidence=float(s_arr[k]),
                    box=BoundingBox(
                        x=float(x1 / orig_w),
                        y=float(y1 / orig_h),
                        w=float((x2 - x1) / orig_w),
                        h=float((y2 - y1) / orig_h),
                    ),
                ),
            )
        return out_dets
