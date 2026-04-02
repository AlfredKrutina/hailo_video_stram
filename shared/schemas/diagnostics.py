from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class CheckSeverity(str, Enum):
    ok = "ok"
    warn = "warn"
    fail = "fail"


class DiagnosticCheck(BaseModel):
    id: str
    severity: CheckSeverity
    ok: bool
    latency_ms: float | None = None
    detail: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class DiagnosticsSummary(BaseModel):
    ok: int = 0
    warn: int = 0
    fail: int = 0


class DiagnosticsReport(BaseModel):
    generated_at: str
    environment: str | None = None
    total_ms: float | None = None
    summary: DiagnosticsSummary
    checks: list[DiagnosticCheck]
