"""Vyhodnocení politiky ukládání oproti jedné detekci."""

from __future__ import annotations

from collections import deque
from time import monotonic

from shared.schemas.detections import Detection
from shared.schemas.recording import RecordingPolicy, filter_attributes_for_storage


def should_persist_detection(det: Detection, policy: RecordingPolicy) -> bool:
    if det.confidence < policy.min_confidence:
        return False
    return det.label.lower().strip() in {x.lower().strip() for x in policy.enabled_labels}


def build_stored_attributes(det: Detection, policy: RecordingPolicy) -> dict:
    label = det.label.lower().strip()
    allowed = policy.attributes_for_label.get(label)
    if allowed is None:
        return {}
    return filter_attributes_for_storage(det.attributes, allowed)


class EventRateLimiter:
    """Sliding window — max N událostí za posledních 60 s."""

    def __init__(self, max_per_minute: int) -> None:
        self._max = max_per_minute
        self._ts: deque[float] = deque()

    def allow(self) -> bool:
        now = monotonic()
        cutoff = now - 60.0
        while self._ts and self._ts[0] < cutoff:
            self._ts.popleft()
        if len(self._ts) >= self._max:
            return False
        self._ts.append(now)
        return True

    def set_max(self, n: int) -> None:
        self._max = max(1, n)
