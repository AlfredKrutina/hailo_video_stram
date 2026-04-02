# raspberry_py_ajax — Architecture

## Processes

| Component | Role |
|-----------|------|
| **ai_core** | GStreamer ingest, inference (Hailo when available), publishes detections + telemetry to Redis, MJPEG upstream for Nginx |
| **web** | FastAPI: Redis + PostgreSQL, REST (včetně politiky ukládání), WebSocket |
| **postgres** | Trvalé uložení `detection_events` a `recording_policy` |
| **redis** | IPC: latest frame metadata, pub/sub, cache politiky pro `ai_core`, volitelný stream náhledu |
| **nginx** | TLS termination, reverse proxy to web API, mirrors MJPEG from ai_core |
| **watchdog** | Monitors Redis heartbeat from ai_core; optional Hailo reset script on crash |

## Detection JSON (Redis key `detections:latest`)

Schema version is embedded in each payload (`schema_version`). Boxes are **normalized** 0–1 relative to frame width/height (`x`, `y`, `w`, `h`).

## RTSP recovery

On TCP/RTSP failure the pipeline transitions to `PAUSED` or `FAILED`, then a background task runs periodic **RTSP DESCRIBE** (HTTP-like request to RTSP URL) with exponential backoff. On success the pipeline is rebuilt or set back to `PLAYING` (strategy: full rebuild for simplicity on Pi).

## Dynamic reconfiguration

`PATCH /api/v1/model` writes Redis key `config:model`. `ai_core` subscribes via Redis pub/sub channel `config:updates` and applies thresholds or triggers a soft pipeline restart.

## Staging vs production

Set `ENVIRONMENT=staging` for verbose structured logging and pipeline diagnostics; `production` reduces noise.

---

## Web UI — co se odkud bere

| Oblast UI | Zdroj dat | Poznámka |
|-----------|-----------|----------|
| Živý obraz | MJPEG z `ai_core` (`/mjpeg/stream.mjpeg` přes Nginx) | Jeden proud; bounding boxy nejsou v obraze, jen JSON + SVG overlay |
| Overlay (rámečky) | Redis `detections:latest` → WebSocket `/ws/telemetry` | Souřadnice normalizované 0–1 |
| Stav pipeline / telemetrie | `telemetry:latest` | Badge, text, grafy (latence, FPS, teploty, bitrate/loss pokud backend plní) |
| Hot-swap zdroje | `PATCH /api/v1/source` → Redis `config:source` → `ai_core` | |
| Práhy modelu | `PATCH /api/v1/model` → `config:model` | Rozšiřitelné v `ModelConfig` |
| Event log | primárně **PostgreSQL** (`GET /api/v1/events`), náhledy `/api/v1/snapshots/{name}` | Redis stream `events:detections` volitelně (`?source=redis`) |
| Politika ukládání | `GET/PUT /api/v1/recording/policy`, katalog `GET /api/v1/recording/catalog` | Uložená politika se zapisuje do DB a do Redis `config:recording_policy`; `ai_core` ji aplikuje při zápisu událostí |

---

## PostgreSQL a konfigurovatelné ukládání

- **Pravda o událostech**: tabulka `detection_events` (čas, frame, zdroj, label, confidence, JSON `attributes`, cesta ke snímku).
- **Politika**: tabulka `recording_policy` (jeden řádek `id=1`, JSON dokument `RecordingPolicy`).
- **Synchronizace**: po uložení politiky ve **web** se Redis aktualizuje a publikuje `config:updates` typ `recording_policy`; **ai_core** načítá politiku z Redis při startu a při zprávě z pub/sub.
- **Limit inference vs politika**: politika určuje *která pole z `Detection.attributes` se uloží*. Hodnoty musí dodat inferenční vrstva — prázdný výběr atributů u daného labelu znamená ukládat jen metadata (bez volitelných polí).

---

## Modely — současný stav a rozšíření (např. SPZ / ALPR)

**Současný kód**

- Inferenční vrstva: **stub** nebo **Hailo** (`services/ai_core/inference/`), podle zařízení a doplnění `hailo_real.py`.
- Výstup: `Detection` v `shared/schemas/detections.py` = `class_id`, `label`, `confidence`, `box` — jedna detekční hlava (např. YOLO: třídy podle trénovacího datasetu).
- Konfigurace: `ModelConfig` v `shared/schemas/config.py` — confidence / IOU; lze doplnit `model_id`, cesty k HEF, přepínače úloh.

**ALPR (SPZ) obvykle není jen „další třída“ ve stejném YOLO**

- **Fáze 1**: detekce registrační desky (bbox).
- **Fáze 2**: OCR na výřezu (text SPZ) — jiný model, často jiná frekvence běhu než plné FPS detekce.

**Směr integrace v této architekturu**

- Rozšířit schéma (např. `schema_version: 2`) o volitelná pole: `attributes: { "plate_text": "…" }` nebo `detection_kind: plate | generic`.
- Nebo oddělený Redis klíč pro pomalejší OCR výsledky (`detections:alpr:latest`), aby se nemíchaly latence.
- Web: zobrazení textu u boxu / v event logu — úprava `services/web/static/app.js` + případně nový API endpoint pro historii SPZ.

Konkrétní modely (Hailo Model Zoo, vlastní HEF, OCR na CPU) závisí na licenci a cílovém HW.
