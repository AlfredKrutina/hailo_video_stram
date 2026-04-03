"""
Volitelný podgraf před RGB vsink — z env `RPY_HAILO_GST_BIN_DESCRIPTION` (Gst.parse_bin_from_description).

Příklad (z TAPPAS / vlastní pipeline): queue ! videoconvert ! ... ! hailonet hef-path=... ! hailofilter ...

Tensorové větve závisí na konkrétní verzi pluginů — neprovádíme odhad NV12 řetězců z Pythonu.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("ai_core.gst.hailo_integration")

_GST = None
try:
    import gi  # noqa: PLC0415

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # noqa: PLC0415

    _GST = Gst
except Exception:
    pass


def try_make_hailo_pre_bin(
    *,
    width: int,
    height: int,
    hef_stage1: str,
) -> Any | None:
    del width, height, hef_stage1
    if _GST is None:
        return None
    Gst = _GST
    desc = os.environ.get("RPY_HAILO_GST_BIN_DESCRIPTION", "").strip()
    if not desc:
        logger.info(
            "hailo_gst_bin_description_missing",
            extra={
                "extra_data": {
                    "hint": "Set RPY_HAILO_GST_BIN_DESCRIPTION to a Gst.parse_bin_from_description fragment "
                    "(e.g. TAPPAS hailonet ! hailofilter ! …) or rely on CPU ONNX path.",
                },
            },
        )
        return None
    try:
        Gst.init(None)
    except Exception:
        pass
    try:
        bin_el = Gst.parse_bin_from_description(desc, True)
        logger.info("hailo_pre_bin_parsed_ok", extra={"extra_data": {"len": len(desc)}})
        return bin_el
    except Exception as e:
        logger.error("hailo_pre_bin_parse_failed", extra={"extra_data": {"err": str(e), "desc": desc[:200]}})
        return None
