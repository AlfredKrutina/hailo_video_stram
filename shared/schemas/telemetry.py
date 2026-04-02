from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class PipelineState(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    RECOVERING = "RECOVERING"
    RECONFIGURING = "RECONFIGURING"
    FAILED = "FAILED"


class TelemetrySnapshot(BaseModel):
    schema_version: int = 1
    pipeline_state: PipelineState = PipelineState.IDLE
    inference_latency_ms: float | None = None
    fps: float | None = None
    soc_temp_c: float | None = None
    hailo_temp_c: float | None = None
    bitrate_kbps: float | None = None
    packet_loss_pct: float | None = None
    camera_connected: bool = False
    last_error: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
