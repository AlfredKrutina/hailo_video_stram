"""Čtení/zápis politiky a událostí."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from services.persistence.models import DetectionEventRow, RecordingPolicyRow
from services.persistence.session import get_engine, session_scope
from shared.schemas.recording import RecordingPolicy, default_policy

logger = logging.getLogger("persistence.recording")

POLICY_SINGLE_ID = 1


def ensure_default_policy_row(session: Session) -> RecordingPolicy:
    row = session.get(RecordingPolicyRow, POLICY_SINGLE_ID)
    if row is None:
        p = default_policy()
        session.add(RecordingPolicyRow(id=POLICY_SINGLE_ID, data=p.model_dump()))
        session.flush()
        return p
    return RecordingPolicy.model_validate(row.data)


def load_policy_from_db() -> RecordingPolicy | None:
    eng = get_engine()
    if not eng:
        return None
    with session_scope() as session:
        return ensure_default_policy_row(session)


def save_policy_to_db(policy: RecordingPolicy) -> None:
    with session_scope() as session:
        row = session.get(RecordingPolicyRow, POLICY_SINGLE_ID)
        payload = policy.model_dump()
        if row is None:
            session.add(RecordingPolicyRow(id=POLICY_SINGLE_ID, data=payload))
        else:
            row.data = payload


def insert_detection_event(
    *,
    frame_id: int,
    source_uri: str,
    label: str,
    class_id: int,
    confidence: float,
    snapshot_path: str | None,
    attributes: dict[str, Any],
) -> int | None:
    eng = get_engine()
    if not eng:
        return None
    with session_scope() as session:
        row = DetectionEventRow(
            frame_id=frame_id,
            source_uri=source_uri,
            label=label.lower(),
            class_id=class_id,
            confidence=confidence,
            snapshot_path=snapshot_path,
            attributes=attributes,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def list_events(
    *,
    limit: int = 50,
    offset: int = 0,
    label: str | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    eng = get_engine()
    if not eng:
        return []
    with session_scope() as session:
        q = select(DetectionEventRow).order_by(desc(DetectionEventRow.created_at))
        if label:
            q = q.where(DetectionEventRow.label == label.lower())
        if since:
            q = q.where(DetectionEventRow.created_at >= since)
        q = q.limit(limit).offset(offset)
        rows = session.scalars(q).all()
        out = []
        for r in rows:
            snap = r.snapshot_path or ""
            name = snap.split("/")[-1] if snap else ""
            out.append(
                {
                    "id": str(r.id),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "frame_id": r.frame_id,
                    "source_uri": r.source_uri,
                    "label": r.label,
                    "class_id": r.class_id,
                    "confidence": r.confidence,
                    "snapshot": snap,
                    "snapshot_name": name,
                    "attributes": dict(r.attributes) if r.attributes else {},
                    "kind": r.label,
                },
            )
        return out


def policy_to_redis_json(policy: RecordingPolicy) -> str:
    return json.dumps(policy.model_dump())
