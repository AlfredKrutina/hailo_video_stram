"""Uvolnění Hailo zařízení když GStreamer nedosáhne stavu NULL včas (zombie / OOPD)."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("ai_core.gst.hailo_release")


def release_hailo_device(redis_publisher: Any | None = None, reason: str = "") -> None:
    """
    Zkusí `hailortcli device release`, případně PCI reset přes sysfs (`RPY_HAILO_PCI_RESET_PATH`).
    Při jakémkoli pokusu publikuje Redis událost `hailo_device_reset`.
    """
    detail = (reason or "")[:2000]
    attempted = False
    ok_cli = False
    cli = shutil.which("hailortcli")
    if cli:
        attempted = True
        try:
            r = subprocess.run(
                [cli, "device", "release"],
                timeout=15,
                capture_output=True,
                text=True,
            )
            ok_cli = r.returncode == 0
            if not ok_cli:
                logger.warning(
                    "hailortcli_release_nonzero",
                    extra={"extra_data": {"code": r.returncode, "stderr": (r.stderr or "")[:500]}},
                )
        except Exception as e:
            logger.warning("hailortcli_release_failed", extra={"extra_data": {"err": str(e)}})

    reset_path = os.environ.get("RPY_HAILO_PCI_RESET_PATH", "").strip()
    ok_reset = False
    if reset_path:
        p = Path(reset_path)
        if p.exists():
            attempted = True
            try:
                p.write_text("1")
                ok_reset = True
            except OSError as e:
                logger.warning("hailo_pci_reset_write_failed", extra={"extra_data": {"path": reset_path, "err": str(e)}})
        else:
            logger.warning("hailo_pci_reset_path_missing", extra={"extra_data": {"path": reset_path}})

    if attempted and redis_publisher is not None:
        try:
            redis_publisher.publish_hailo_device_reset_event(detail, hailortcli_ok=ok_cli, pci_reset_ok=ok_reset)
        except Exception as e:
            logger.debug("redis_hailo_reset_publish_failed", extra={"extra_data": {"err": str(e)}})
