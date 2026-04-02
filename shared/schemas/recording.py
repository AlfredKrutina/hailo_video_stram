"""Katalog atributů a uživatelská politika ukládání událostí."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class AttributeId(str, Enum):
    """Normalizované klíče do `Detection.attributes` a DB JSON."""

    PLATE_TEXT = "plate_text"
    VEHICLE_MAKE = "vehicle_make"
    VEHICLE_MODEL = "vehicle_model"
    VEHICLE_COLOR = "vehicle_color"
    PERSON_UPPER_COLOR = "person_upper_color"
    PERSON_LOWER_COLOR = "person_lower_color"
    PERSON_OUTER_COLOR = "person_outer_color"


class CatalogAttribute(BaseModel):
    id: str
    label_cs: str
    value_hint: str = "string"
    requires_capability: bool = False


class CatalogEntity(BaseModel):
    """Skupina (např. osoba / vozidlo) — výběr labelů a atributů v UI."""

    id: str
    label_cs: str
    """Synonyma labelů z modelu (lowercase)."""
    match_labels: list[str] = Field(default_factory=list)
    attributes: list[CatalogAttribute] = Field(default_factory=list)


class RecordingCatalog(BaseModel):
    """Jedna pravda pro API a validaci politiky."""

    version: int = 1
    entities: list[CatalogEntity] = Field(default_factory=list)

    def all_attribute_ids(self) -> set[str]:
        out: set[str] = set()
        for e in self.entities:
            for a in e.attributes:
                out.add(a.id)
        return out

    def labels_for_entity(self, entity_id: str) -> list[str]:
        for e in self.entities:
            if e.id == entity_id:
                return list(e.match_labels)
        return []


def default_catalog() -> RecordingCatalog:
    return RecordingCatalog(
        version=1,
        entities=[
            CatalogEntity(
                id="person",
                label_cs="Osoba",
                match_labels=["person", "člověk"],
                attributes=[
                    CatalogAttribute(
                        id=AttributeId.PERSON_UPPER_COLOR.value,
                        label_cs="Barva trička / horní část",
                        requires_capability=True,
                    ),
                    CatalogAttribute(
                        id=AttributeId.PERSON_LOWER_COLOR.value,
                        label_cs="Barva kalhot / spodní část",
                        requires_capability=True,
                    ),
                    CatalogAttribute(
                        id=AttributeId.PERSON_OUTER_COLOR.value,
                        label_cs="Barva bundy / svrchní vrstva",
                        requires_capability=True,
                    ),
                ],
            ),
            CatalogEntity(
                id="vehicle",
                label_cs="Vozidlo",
                match_labels=["car", "truck", "bus", "vehicle", "auto"],
                attributes=[
                    CatalogAttribute(
                        id=AttributeId.VEHICLE_MAKE.value,
                        label_cs="Značka / model (zjednodušeně)",
                        requires_capability=True,
                    ),
                    CatalogAttribute(
                        id=AttributeId.VEHICLE_MODEL.value,
                        label_cs="Model vozidla",
                        requires_capability=True,
                    ),
                    CatalogAttribute(
                        id=AttributeId.VEHICLE_COLOR.value,
                        label_cs="Barva vozidla",
                        requires_capability=True,
                    ),
                    CatalogAttribute(
                        id=AttributeId.PLATE_TEXT.value,
                        label_cs="Text SPZ (OCR)",
                        requires_capability=True,
                    ),
                ],
            ),
        ],
    )


class RecordingPolicy(BaseModel):
    """Co ukládat — serializovat do DB a Redis."""

    min_confidence: float = Field(default=0.45, ge=0.0, le=1.0)
    store_snapshots: bool = True
    """Label (lowercase) které ukládat — prázdné = nic neukládat."""
    enabled_labels: list[str] = Field(default_factory=lambda: ["person"])
    """label -> seznam AttributeId k uložení z detekce; prázdný seznam = žádné volitelné atributy."""
    attributes_for_label: dict[str, list[str]] = Field(default_factory=dict)
    max_events_per_minute: int = Field(default=120, ge=1, le=10000)

    @model_validator(mode="after")
    def normalize_labels(self) -> RecordingPolicy:
        self.enabled_labels = [x.lower().strip() for x in self.enabled_labels if x.strip()]
        low: dict[str, list[str]] = {}
        for k, v in self.attributes_for_label.items():
            kk = k.lower().strip()
            low[kk] = list(dict.fromkeys(v))
        self.attributes_for_label = low
        return self


def default_policy() -> RecordingPolicy:
    return RecordingPolicy(
        enabled_labels=["person"],
        attributes_for_label={
            "person": [
                AttributeId.PERSON_UPPER_COLOR.value,
                AttributeId.PERSON_LOWER_COLOR.value,
            ],
        },
    )


def filter_attributes_for_storage(
    raw: dict[str, Any] | None,
    allowed: list[str],
) -> dict[str, Any]:
    if not raw or not allowed:
        return {}
    allow = set(allowed)
    return {k: v for k, v in raw.items() if k in allow}
