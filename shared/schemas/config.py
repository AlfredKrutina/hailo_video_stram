from __future__ import annotations

from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    confidence_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    iou_threshold: float = Field(default=0.45, ge=0.0, le=1.0)


class SourceConfig(BaseModel):
    """Active ingest URI (RTSP, file, http HLS, etc.)."""

    uri: str = "rtsp://127.0.0.1:8554/stream"
    label: str = "default"


class AppConfig(BaseModel):
    redis_url: str = "redis://redis:6379/0"
    snapshot_dir: str = "/data/snapshots"
    model: ModelConfig = Field(default_factory=ModelConfig)
    source: SourceConfig = Field(default_factory=SourceConfig)
    mjpeg_port: int = 8081
    heartbeat_interval_s: float = 2.0
    use_hailo: bool = True
