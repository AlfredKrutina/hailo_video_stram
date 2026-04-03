"""
Volitelný podgraf před RGB vsink — z env `RPY_HAILO_GST_BIN_DESCRIPTION` (Gst.parse_bin_from_description).

Výchozí řetězec převádí video na RGB před `hailonet` (YUV z decode → RGB), doplní `hef-path` a volitelně `so-path` z env.
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

# Dosazuje se v Pythonu: {RPY_HAILO_HEF_STAGE1}, {RPY_HAILO_FILTER_SO}
_DEFAULT_HAILO_GST_BIN_DESCRIPTION = (
    "videoconvert ! video/x-raw,format=RGB ! "
    "hailonet hef-path={RPY_HAILO_HEF_STAGE1} ! "
    "hailofilter so-path={RPY_HAILO_FILTER_SO} ! hailooverlay ! videoconvert"
)


def _substitute_hailo_bin_description(desc: str, hef_stage1: str) -> str:
    out = desc.replace("{RPY_HAILO_HEF_STAGE1}", hef_stage1)
    filt = (
        os.environ.get("RPY_HAILO_FILTER_SO", "").strip()
        or os.environ.get("RPY_HAILO_FILTER_SO_PATH", "").strip()
    )
    out = out.replace("{RPY_HAILO_FILTER_SO}", filt)
    return out


def try_make_hailo_pre_bin(
    *,
    width: int,
    height: int,
    hef_stage1: str,
) -> Any | None:
    del width, height
    if _GST is None:
        return None
    Gst = _GST
    raw = os.environ.get("RPY_HAILO_GST_BIN_DESCRIPTION", "").strip()
    using_default = not raw
    desc = raw or _DEFAULT_HAILO_GST_BIN_DESCRIPTION
    desc = _substitute_hailo_bin_description(desc, hef_stage1)
    if using_default:
        filt = (
            os.environ.get("RPY_HAILO_FILTER_SO", "").strip()
            or os.environ.get("RPY_HAILO_FILTER_SO_PATH", "").strip()
        )
        if not filt:
            logger.error(
                "hailo_default_gst_bin_requires_filter_so",
                extra={
                    "extra_data": {
                        "hint": "Set RPY_HAILO_FILTER_SO (postprocess .so) or override RPY_HAILO_GST_BIN_DESCRIPTION.",
                    },
                },
            )
            return None
    if "{RPY_HAILO_FILTER_SO}" in desc or "{RPY_HAILO_HEF_STAGE1}" in desc:
        logger.error(
            "hailo_gst_bin_unsubstituted_placeholder",
            extra={"extra_data": {"hint": "Set RPY_HAILO_FILTER_SO or full RPY_HAILO_GST_BIN_DESCRIPTION"}},
        )
        return None
    if not desc:
        logger.info(
            "hailo_gst_bin_description_missing",
            extra={
                "extra_data": {
                    "hint": "Set RPY_HAILO_GST_BIN_DESCRIPTION or RPY_HAILO_HEF_STAGE1 + RPY_HAILO_FILTER_SO.",
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
