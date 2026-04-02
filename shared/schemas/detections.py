from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


class BoundingBox(BaseModel):
    """Normalized coordinates 0..1 relative to frame dimensions."""

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(ge=0.0, le=1.0)
    h: float = Field(ge=0.0, le=1.0)


class Detection(BaseModel):
    class_id: int
    label: str
    confidence: float = Field(ge=0.0, le=1.0)
    box: BoundingBox
    """Volitelné atributy z modelu (barvy, SPZ, …) — klíče jako v `AttributeId`."""
    attributes: dict[str, Any] | None = None


class DetectionFrame(BaseModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    frame_id: int
    timestamp_ns: int
    width: int
    height: int
    source_uri: str
    detections: list[Detection]

    def model_dump_json_round(self) -> str:
        return self.model_dump_json()
