"""Kontrola dostupnosti GStreamer pluginů Hailo (hailonet) pro telemetrii."""

from __future__ import annotations

import logging

logger = logging.getLogger("ai_core.inference.hailo_gst_probe")

_GST_OK = False
try:
    import gi  # noqa: PLC0415

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # noqa: PLC0415

    _GST_OK = True
except Exception as e:
    logger.debug("hailo_gst_probe_no_gst", extra={"extra_data": {"err": str(e)}})
    Gst = None  # type: ignore[misc, assignment]


def hailonet_element_available() -> bool:
    if not _GST_OK or Gst is None:
        return False
    try:
        Gst.init(None)
    except Exception:
        pass
    try:
        fac = Gst.ElementFactory.find("hailonet")
        return fac is not None
    except Exception:
        return False
