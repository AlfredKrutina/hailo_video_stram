"""
Callback kontext pro Hailo TAPPAS / hailofilter (Python postprocess).

Nastavte před PLAYING: `set_hailo_filter_context(...)`.
Skutečný podpis callbacku závisí na verzi libhailo_postprocess / TAPPAS — upravte podle
hailo-rpi5-examples (detection.py).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from shared.schemas.config import ModelConfig
from shared.schemas.detections import BoundingBox, Detection, DetectionFrame

from services.ai_core.inference.hailo_stage2_runner import Stage2Runner

logger = logging.getLogger("ai_core.inference.hailo_filter_callbacks")

_COCO_PERSON = 0
_COCO_CAR = 2

_ctx_lock = threading.Lock()
_ctx: dict[str, Any] = {}


def set_hailo_filter_context(
    *,
    publish: Callable[[DetectionFrame], None],
    source_uri: str,
    model: ModelConfig,
    width: int,
    height: int,
) -> None:
    with _ctx_lock:
        _ctx.clear()
        _ctx["publish"] = publish
        _ctx["source_uri"] = source_uri
        _ctx["model"] = model
        _ctx["width"] = width
        _ctx["height"] = height
        _ctx["stage2"] = Stage2Runner()
        _ctx["frame_id"] = 0


def _norm_box(x1: float, y1: float, x2: float, y2: float, w: int, h: int) -> BoundingBox:
    return BoundingBox(
        x=max(0.0, min(1.0, x1 / w)),
        y=max(0.0, min(1.0, y1 / h)),
        w=max(0.0, min(1.0, (x2 - x1) / w)),
        h=max(0.0, min(1.0, (y2 - y1) / h)),
    )


def detections_from_yolo_heads(
    metadata: Any,
    *,
    image_width: int,
    image_height: int,
) -> list[Detection]:
    """
    Převod metadat z Hailo YOLO postprocess na interní Detection.
    `metadata` je zástupný typ — nahraďte strukturou z vaší verze TAPPAS / buffer meta.
    """
    del metadata
    return []


def build_frame_and_publish(
    detections: list[Detection],
    frame_id: int,
) -> None:
    with _ctx_lock:
        pub = _ctx.get("publish")
        src = str(_ctx.get("source_uri", ""))
        w = int(_ctx.get("width", 640))
        h = int(_ctx.get("height", 480))
    if not callable(pub):
        return
    ts = time.time_ns()
    frame = DetectionFrame(
        frame_id=frame_id,
        timestamp_ns=ts,
        width=w,
        height=h,
        source_uri=src,
        detections=detections,
    )
    try:
        pub(frame)
    except Exception as e:
        logger.error("hailo_publish_detections_failed", extra={"extra_data": {"err": str(e)}})


# TAPPAS často exportuje funkci jménem podle configu — alias pro Gst parse:
def hailo_app_callback(pad: Any, info: Any, user_data: Any) -> Any:
    """
    GstPadProbeCallback styl — pokud hailofilter používá .so, tato funkce se nevolá.
    Držíme jako šablonu pro pythonové větve.
    """
    del pad, info, user_data
    with _ctx_lock:
        _ctx["frame_id"] = int(_ctx.get("frame_id", 0)) + 1
        fid = _ctx["frame_id"]
    build_frame_and_publish([], fid)
    return 1  # GST_PAD_PROBE_OK
