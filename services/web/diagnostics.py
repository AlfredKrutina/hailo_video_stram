"""
Aggregated stack diagnostics for operators (Redis, Postgres, ai_core heartbeat, MJPEG upstream).

Called from GET /api/v1/diagnostics. Uses sync Redis + sync DB in an async route (acceptable for rare manual runs).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import redis
from sqlalchemy import text

from shared.schemas.diagnostics import (
    CheckSeverity,
    DiagnosticCheck,
    DiagnosticsReport,
    DiagnosticsSummary,
)
from shared.schemas.telemetry import PipelineState, TelemetrySnapshot
from services.persistence.session import get_database_url, get_engine, session_scope

logger = logging.getLogger("web.diagnostics")

AI_HEARTBEAT_KEY = "ai:heartbeat"
AI_HEARTBEAT_TTL_S = 10
TELEMETRY_KEY = "telemetry:latest"
REDIS_BENCH_ROUNDS = 200
MJPEG_READ_CAP = 65536
MJPEG_TIMEOUT_S = 6.0

_DEFAULT_MJPEG_URL = "http://ai_core:8081/stream.mjpeg"


def _mjpeg_url() -> str:
    return os.environ.get("RPY_AI_CORE_MJPEG_URL", _DEFAULT_MJPEG_URL).strip() or _DEFAULT_MJPEG_URL


def _summarize(checks: list[DiagnosticCheck]) -> DiagnosticsSummary:
    s = DiagnosticsSummary()
    for c in checks:
        if c.severity == CheckSeverity.ok:
            s.ok += 1
        elif c.severity == CheckSeverity.warn:
            s.warn += 1
        else:
            s.fail += 1
    return s


def _check_ai_stack_from_telemetry(snap: TelemetrySnapshot) -> DiagnosticCheck:
    """Čte telemetry.extra z ai_core: infer backend, ingress, Hailo zařízení."""
    t0 = time.perf_counter()
    ex = snap.extra or {}
    active = ex.get("infer_backend_active")
    if active is None:
        return DiagnosticCheck(
            id="ai_infer_stack",
            severity=CheckSeverity.warn,
            ok=False,
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            detail="telemetrie bez infer_backend_active — starší ai_core image?",
            data={"ingress_mode": ex.get("ingress_mode")},
        )
    ingress = ex.get("ingress_mode") or ex.get("rtsp_mode")
    hailo_pres = ex.get("hailo_device_present")
    note = ex.get("infer_backend_note")
    gst_err = (ex.get("last_gst_error") or "")[:120]
    hw = ex.get("gst_hw_decode_hint")
    data: dict[str, Any] = {
        "infer_backend_active": active,
        "ingress_mode": ingress,
        "hailo_device_present": hailo_pres,
        "hailo_infer_implemented": ex.get("hailo_infer_implemented"),
        "gst_hw_decode_hint": hw or None,
    }
    if note:
        data["infer_backend_note"] = str(note)[:400]

    sev = CheckSeverity.ok
    ok = True
    parts: list[str] = []
    if active == "stub":
        sev = CheckSeverity.warn
        ok = False
        parts.append("aktivní stub inference (žádný ONNX / plný Hailo)")
    elif active == "hailo":
        parts.append("Hailo backend")
        impl = ex.get("hailo_infer_implemented")
        if impl is False:
            sev = CheckSeverity.warn
            ok = False
            parts.append("hailo_infer_implemented=false (starší build nebo nekompletní backend)")
        if hailo_pres is False:
            sev = CheckSeverity.warn
            ok = False
            parts.append("Hailo zařízení v telemetrii absent")
    elif active == "onnx":
        parts.append("ONNX CPU")
    if gst_err:
        sev = CheckSeverity.warn if sev == CheckSeverity.ok else sev
        ok = False
        parts.append(f"gst: {gst_err}")
    detail = "; ".join(parts) if parts else f"backend={active or '?'}"
    return DiagnosticCheck(
        id="ai_infer_stack",
        severity=sev,
        ok=ok,
        latency_ms=round((time.perf_counter() - t0) * 1000, 2),
        detail=detail[:500],
        data=data,
    )


def _telemetry_severity(state: PipelineState) -> tuple[CheckSeverity, bool, str]:
    if state == PipelineState.FAILED:
        return CheckSeverity.fail, False, str(state.value)
    if state == PipelineState.RECOVERING:
        return CheckSeverity.warn, False, str(state.value)
    if state == PipelineState.RUNNING:
        return CheckSeverity.ok, True, str(state.value)
    return CheckSeverity.warn, False, str(state.value)


async def _check_mjpeg_upstream() -> DiagnosticCheck:
    url = _mjpeg_url()
    t0 = time.perf_counter()
    detail: str | None = None
    data: dict[str, Any] = {"url_host": url.split("://", 1)[-1].split("/", 1)[0]}
    severity = CheckSeverity.fail
    ok = False
    buf = b""

    try:
        timeout = httpx.Timeout(MJPEG_TIMEOUT_S, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", url) as response:
                data["http_status"] = response.status_code
                if response.status_code != 200:
                    detail = f"HTTP {response.status_code}"
                    return DiagnosticCheck(
                        id="mjpeg_upstream",
                        severity=severity,
                        ok=False,
                        latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                        detail=detail,
                        data=data,
                    )
                async for chunk in response.aiter_bytes():
                    buf += chunk
                    if (
                        len(buf) >= MJPEG_READ_CAP
                        or b"--frame" in buf
                        or b"\xff\xd8\xff" in buf
                    ):
                        break
    except httpx.TimeoutException:
        detail = f"timeout po {MJPEG_TIMEOUT_S}s, přečteno {len(buf)} B"
    except httpx.ConnectError as e:
        detail = f"connect error: {e}"
    except OSError as e:
        detail = str(e)
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    data["bytes_read"] = len(buf)

    if detail is None:
        if len(buf) == 0:
            detail = "0 bajtů těla (fronta prázdná / čekání na snímek)"
            severity = CheckSeverity.fail
            ok = False
        elif b"--frame" in buf or b"\xff\xd8" in buf:
            detail = "multipart nebo JPEG data v prvním bloku"
            severity = CheckSeverity.ok
            ok = True
        else:
            detail = "neočekávaný obsah (není boundary ani JPEG)"
            severity = CheckSeverity.warn
            ok = False

    return DiagnosticCheck(
        id="mjpeg_upstream",
        severity=severity,
        ok=ok,
        latency_ms=elapsed_ms,
        detail=detail,
        data=data,
    )


def _check_host_load() -> DiagnosticCheck:
    path = Path("/proc/loadavg")
    t0 = time.perf_counter()
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return DiagnosticCheck(
            id="host_load",
            severity=CheckSeverity.ok,
            ok=True,
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            detail="skipped (no /proc/loadavg)",
            data={"path": str(path)},
        )
    parts = raw.split()
    data: dict[str, Any] = {"loadavg": raw[:200]}
    if len(parts) >= 3:
        try:
            data["load1"] = float(parts[0])
            data["load5"] = float(parts[1])
            data["load15"] = float(parts[2])
        except ValueError:
            pass
    return DiagnosticCheck(
        id="host_load",
        severity=CheckSeverity.ok,
        ok=True,
        latency_ms=round((time.perf_counter() - t0) * 1000, 2),
        detail=raw[:80],
        data=data,
    )


async def collect_diagnostics(
    redis_client: redis.Redis,
    *,
    db_ok: bool,
    environment: str | None,
) -> DiagnosticsReport:
    """
    Run all checks. Raises redis.RedisError if Redis is unreachable (caller → 503).
    """
    wall0 = time.perf_counter()
    checks: list[DiagnosticCheck] = []

    t0 = time.perf_counter()
    redis_client.ping()
    checks.append(
        DiagnosticCheck(
            id="redis_ping",
            severity=CheckSeverity.ok,
            ok=True,
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            detail="PONG",
            data={},
        ),
    )

    t0 = time.perf_counter()
    hb_raw = redis_client.get(AI_HEARTBEAT_KEY)
    hb_ms = round((time.perf_counter() - t0) * 1000, 2)
    if not hb_raw:
        checks.append(
            DiagnosticCheck(
                id="redis_ai_heartbeat",
                severity=CheckSeverity.warn,
                ok=False,
                latency_ms=hb_ms,
                detail="klíč ai:heartbeat chybí (ai_core neběží nebo nezapisuje)",
                data={"key": AI_HEARTBEAT_KEY},
            ),
        )
    else:
        try:
            ts = float(hb_raw)
            age = time.time() - ts
            stale = age > float(AI_HEARTBEAT_TTL_S) * 1.5
            sev = CheckSeverity.warn if stale else CheckSeverity.ok
            checks.append(
                DiagnosticCheck(
                    id="redis_ai_heartbeat",
                    severity=sev,
                    ok=not stale,
                    latency_ms=hb_ms,
                    detail=f"stáří ~{age:.1f}s (TTL {AI_HEARTBEAT_TTL_S}s)",
                    data={"age_s": round(age, 2)},
                ),
            )
        except ValueError:
            checks.append(
                DiagnosticCheck(
                    id="redis_ai_heartbeat",
                    severity=CheckSeverity.warn,
                    ok=False,
                    latency_ms=hb_ms,
                    detail="nečitelná hodnota heartbeat",
                    data={"raw": hb_raw[:80]},
                ),
            )

    t0 = time.perf_counter()
    tel_raw = redis_client.get(TELEMETRY_KEY)
    tel_ms = round((time.perf_counter() - t0) * 1000, 2)
    if not tel_raw:
        checks.append(
            DiagnosticCheck(
                id="redis_telemetry",
                severity=CheckSeverity.warn,
                ok=False,
                latency_ms=tel_ms,
                detail="telemetry:latest chybí",
                data={},
            ),
        )
    else:
        try:
            snap = TelemetrySnapshot.model_validate_json(tel_raw)
            sev, ok, state_label = _telemetry_severity(snap.pipeline_state)
            err_short = (snap.last_error or "")[:240]
            detail = f"pipeline={state_label}"
            if err_short:
                detail += f" | {err_short}"
            extra_preview = {k: snap.extra.get(k) for k in list(snap.extra)[:8]}
            checks.append(
                DiagnosticCheck(
                    id="redis_telemetry",
                    severity=sev,
                    ok=ok and sev == CheckSeverity.ok,
                    latency_ms=tel_ms,
                    detail=detail,
                    data={
                        "pipeline_state": state_label,
                        "camera_connected": snap.camera_connected,
                        "fps": snap.fps,
                        "extra_keys": list(snap.extra.keys())[:20],
                        "extra_preview": extra_preview,
                    },
                ),
            )
            checks.append(_check_ai_stack_from_telemetry(snap))
        except Exception as e:
            checks.append(
                DiagnosticCheck(
                    id="redis_telemetry",
                    severity=CheckSeverity.warn,
                    ok=False,
                    latency_ms=tel_ms,
                    detail=f"parsování selhalo: {e}",
                    data={},
                ),
            )

    if not get_database_url():
        checks.append(
            DiagnosticCheck(
                id="postgres",
                severity=CheckSeverity.ok,
                ok=True,
                latency_ms=None,
                detail="DATABASE_URL není nastaveno",
                data={"skipped": True},
            ),
        )
    elif not db_ok:
        checks.append(
            DiagnosticCheck(
                id="postgres",
                severity=CheckSeverity.fail,
                ok=False,
                latency_ms=None,
                detail="degraded (startup seed / migrace)",
                data={},
            ),
        )
    else:
        t0 = time.perf_counter()
        try:
            if get_engine() is None:
                raise RuntimeError("no engine")
            with session_scope() as session:
                session.execute(text("SELECT 1"))
            checks.append(
                DiagnosticCheck(
                    id="postgres",
                    severity=CheckSeverity.ok,
                    ok=True,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    detail="SELECT 1 ok",
                    data={},
                ),
            )
        except Exception as e:
            checks.append(
                DiagnosticCheck(
                    id="postgres",
                    severity=CheckSeverity.fail,
                    ok=False,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    detail=str(e)[:200],
                    data={},
                ),
            )

    bench_key = f"diag:bench:{uuid.uuid4().hex}"
    t0 = time.perf_counter()
    try:
        for _ in range(REDIS_BENCH_ROUNDS):
            redis_client.set(bench_key, "1")
            redis_client.get(bench_key)
        redis_client.delete(bench_key)
        elapsed = time.perf_counter() - t0
        ops = (REDIS_BENCH_ROUNDS * 2) / elapsed if elapsed > 0 else 0.0
        checks.append(
            DiagnosticCheck(
                id="redis_microbench",
                severity=CheckSeverity.ok,
                ok=True,
                latency_ms=round(elapsed * 1000, 2),
                detail=f"{REDIS_BENCH_ROUNDS}× SET+GET",
                data={"ops_per_s": round(ops, 1), "rounds": REDIS_BENCH_ROUNDS},
            ),
        )
    except redis.RedisError as e:
        checks.append(
            DiagnosticCheck(
                id="redis_microbench",
                severity=CheckSeverity.fail,
                ok=False,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                detail=str(e),
                data={},
            ),
        )

    checks.append(_check_host_load())

    checks.append(await _check_mjpeg_upstream())

    total_ms = round((time.perf_counter() - wall0) * 1000, 2)
    summary = _summarize(checks)
    report = DiagnosticsReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        environment=environment,
        total_ms=total_ms,
        summary=summary,
        checks=checks,
    )

    logger.info(
        "diagnostics_run",
        extra={
            "extra_data": {
                "total_ms": total_ms,
                "summary_ok": summary.ok,
                "summary_warn": summary.warn,
                "summary_fail": summary.fail,
            },
        },
    )
    return report
