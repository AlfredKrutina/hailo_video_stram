# Struktura projektu `hailo_video_stram`

Monorepo pro **Raspberry Pi 5** (nebo PC) s Dockerem: oddělené služby **ai_core** (GStreamer + inference), **web** (FastAPI + SPA), **Redis**, **Postgres**, **nginx**.

## Kořen repozitáře

| Cesta | Účel |
|--------|------|
| `pyproject.toml` | Python balíček (editable install v imagech) |
| `.env.example` | Šablona proměnných pro compose (zdroj videa, Hailo, `RPY_AI_CORE_VIDEO_WS_URL`, …) |
| `config/` | YAML konfigurace aplikace (`default.yaml` + env přepisy) |
| `docker/` | `Dockerfile.ai`, `Dockerfile.web`, `docker-compose.yml` |
| `nginx/` | `nginx.conf` — proxy :80 → `web:8080`, dlouhé timeouty pro `/ws/` |
| `scripts/` | `smoke_stack.sh`, `ci_check.sh`, `watchdog.sh` |
| `shared/` | Sdílené schématy, chybové kódy, logging |
| `services/` | Hlavní aplikační kód (`ai_core`, `web`, `persistence`) |
| `docs/` | Architektura, nasazení, smoke testy, poznámky k Hailo |
| `context/` | Lokální poznámky / audit (mimo runtime) |

## `services/ai_core/`

| Soubor / modul | Role |
|----------------|------|
| `run_core.py` | Hlavní proces: Redis, telemetry vlákna, GStreamer pipeline, fronta JPEG |
| `video_ws_server.py` | aiohttp WS `GET /ws/video` — broadcast `0x01`+JPEG, max ~25 fps |
| `pipeline/gst_pipeline.py` | GStreamer graf, stavy, teardown (`NULL` + `get_state`) |
| `pipeline/hailo_device_release.py` | Uvolnění Hailo zařízení při recovery |
| `inference/` | Factory ONNX / Hailo / stub |
| `ipc/redis_pub.py` | Publikace telemetrie, detekcí, config subscriber |
| `config/load.py` | Načtení `AppConfig` z YAML + env |

**Port:** uvnitř Docker sítě typicky **8081** pro video WS (`mjpeg_port` v YAML — historický název pole).

## `services/web/`

| Soubor / modul | Role |
|----------------|------|
| `app.py` | FastAPI: REST, `/ws/telemetry` (JSON z Redis + binární video), `/health`, statika |
| `ws_video_bridge.py` | Singleton klient `ws://ai_core:8081/ws/video` → poslední snímek pro forward |
| `diagnostics.py` | `/api/v1/diagnostics` — Redis, DB, ai_core heartbeat, WS dostupnost |
| `static/` | `index.html`, `app.js`, `styles.css` — canvas + jeden WebSocket |
| `recording_api.py` | Validace politiky vůči katalogu |

**Port:** **8080** (uvicorn); uživatel má jít přes **nginx :80**.

## `services/persistence/`

SQLAlchemy modely, `session.py` (`init_db`, `session_scope`), `recording_store.py` pro události a politiku.

## Tok dat (zjednodušeně)

1. **ai_core** čte video, inferuje, zapisuje `telemetry:latest` / `detections:latest` do Redis, posílá JPEG do fronty → **video_ws_server** → klient `web`.
2. **web** v `ws_video_bridge` drží poslední binární zprávu; `/ws/telemetry` čte Redis (v thread poolu) a posílá JSON + binární snímky prohlížeči.
3. **Prohlížeč** jeden WS: string → telemetrie + overlay; ArrayBuffer → JPEG na `<canvas>`.

## Provozní poznámka (502)

**502 na `http://IP/`** znamená, že nginx nedosáhne `web:8080` (proces nenaběhl, pád při importu, neposlouchá). Otevírejte UI jako **`http://IP/`** (port 80), ne mix s `:8080`, aby seděl origin pro WebSocket.
