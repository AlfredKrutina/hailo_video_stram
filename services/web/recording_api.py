"""Validace politiky vůči katalogu."""

from __future__ import annotations

from fastapi import HTTPException

from shared.schemas.recording import RecordingCatalog, RecordingPolicy, default_catalog


def validate_policy_against_catalog(policy: RecordingPolicy, catalog: RecordingCatalog) -> None:
    allowed = catalog.all_attribute_ids()
    for _label, attrs in policy.attributes_for_label.items():
        for a in attrs:
            if a not in allowed:
                raise HTTPException(
                    status_code=400,
                    detail=f"Neznámý atribut: {a}",
                )
