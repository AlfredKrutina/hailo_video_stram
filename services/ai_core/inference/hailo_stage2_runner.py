"""
Stage 2 HEF modely (LPR, barva auta, atributy vozidla, barvy oblečení) — volitelně přes hailo_platform.

Spouští se z hailofilter callbacku / postprocess vlákna; při chybějícím HailoRT vrací prázdné attrs.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import numpy as np

logger = logging.getLogger("ai_core.inference.hailo_stage2")


def _hef(name: str) -> str:
    return os.environ.get(name, "").strip()


class Stage2Runner:
    """Načte cesty k HEF z env; infer ROI lazy při prvním volání."""

    def __init__(self) -> None:
        self.hef_lpr = _hef("RPY_HAILO_HEF_LPR")
        self.hef_vcolor = _hef("RPY_HAILO_HEF_VCOLOR")
        self.hef_vattr = _hef("RPY_HAILO_HEF_VATTR")
        self.hef_pattr = _hef("RPY_HAILO_HEF_PATTR")
        self._warned = False

    def enrich_car(
        self,
        roi_rgb: np.ndarray,
        base_attrs: dict[str, Any],
    ) -> dict[str, Any]:
        out = dict(base_attrs)
        if not self.hef_lpr and not self.hef_vcolor and not self.hef_vattr:
            return out
        if not self._warned:
            self._warned = True
            logger.warning(
                "hailo_stage2_stub",
                extra={
                    "extra_data": {
                        "msg": "Stage2 HEF inference není v tomto buildu plně napojena — doplňte HailoRT + parsování výstupů podle Model Zoo.",
                        "lpr": bool(self.hef_lpr),
                        "vcolor": bool(self.hef_vcolor),
                        "vattr": bool(self.hef_vattr),
                    },
                },
            )
        # Placeholder — produkční kód: VDevice + InferVStreams na ROI podle dokumentace k danému HEF
        return out

    def enrich_person(
        self,
        roi_rgb: np.ndarray,
        base_attrs: dict[str, Any],
    ) -> dict[str, Any]:
        out = dict(base_attrs)
        if not self.hef_pattr:
            return out
        return out
