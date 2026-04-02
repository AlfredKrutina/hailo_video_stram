# Výstup auditu: raspberry_py_ajax (podle plánu)

**Datum:** 2026-04-02  
**Prostředí ověření:** vývojový Windows host — Docker Desktop neběžel (`npipe` k daemonu nedostupný); živý stack v tomto běhu nebyl spuštěný.

---

## 1. Runtime ověření (Fáze 1 plánu)

### Co bylo provedeno zde

- `docker compose ps` — **selhalo** (Docker API nedostupné).
- `curl.exe` na `http://127.0.0.1:8080/api/v1/diagnostics` — vráceno **HTML 404** (na portu 8080 zde neběží tento FastAPI stack, nejspíš jiná služba).
- `curl.exe` na `http://127.0.0.1/health` — **timeout** (nic na :80 neodpovídalo v čase 2 s).

**Závěr:** Skutečné hodnoty kontrol `mjpeg_upstream`, `redis_ai_heartbeat`, `redis_telemetry` a `mjpeg_browser_ttfb` z tohoto běhu **nejsou k dispozici**. Je nutné zopakovat na cílovém stroji se spuštěným compose.

### Postup k doplnění faktů (operátor)

1. UI **vždy přes Nginx** — `http://<host>/` (port **80**), ne přímo `web:8080`. Jinak `/mjpeg/stream.mjpeg` končí na FastAPI bez MJPEG.
2. V UI: **„Spustit diagnostiku stacku“** — kombinuje `GET /api/v1/diagnostics` a měření TTFB MJPEG v prohlížeči.
3. CLI ekvivalent (uvnitř sítě kde běží stack):

   ```bash
   curl -sS http://127.0.0.1/health
   curl -sS http://127.0.0.1/api/v1/diagnostics | jq .
   ```

4. Rozšířený checklist je v [context/terminal_diagnostics.txt](terminal_diagnostics.txt).

### Mapování výsledků diagnostiky na příčiny

| Kontrola | Fail / warn typicky znamená |
|----------|-----------------------------|
| `mjpeg_upstream` · 0 B / timeout | Prázdná JPEG fronta, ai_core down, nebo `web` nevidí `ai_core:8081`. |
| `redis_ai_heartbeat` | ai_core nevolá `RedisPublisher.heartbeat()` (proces mrtvý / zaseknutý před smyčkou). |
| `redis_telemetry` · `FAILED` / `last_error` | GStreamer / zdroj / 403 streak — detail v telemetrii a logu ai_core. |
| `mjpeg_browser_ttfb` špatně při OK serveru | Cesta Nginx → prohlížeč, nejen backend. |

---

## 2. Konfigurace a drift (Fáze 2)

| Téma | Zjištění |
|------|----------|
| **SOURCE_URI** | V [docker/docker-compose.yml](../docker/docker-compose.yml) je default `SOURCE_URI: ${SOURCE_URI:-file:///opt/rpy/assets/sample.mp4}` — **soulad** s [config/default.yaml](../config/default.yaml) (`file:///opt/rpy/assets/sample.mp4`). Přepis: env `SOURCE_URI` má přednost ([services/ai_core/config/load.py](../services/ai_core/config/load.py)). |
| **USE_HAILO** | Při `1` a selhání importu Hailo → **StubHailoBackend** ([hailo_backend.py](../services/ai_core/inference/hailo_backend.py)) — video může běžet, detekce neodpovídají reálnému HW. |
| **RPY_AI_CORE_MJPEG_URL** | V compose pro `web` je nastaveno `http://ai_core:8081/stream.mjpeg` — pro diagnostiku uvnitř Docker sítě **správně**. Při lokálním spuštění `web` mimo compose nutné nastavit env ručně. |
| **PostgreSQL / Redis** | `DATABASE_URL` a `REDIS_URL` konzistentní mezi službami ve stejném souboru compose. |

**Potenciální zmatek pro člověka:** očekávat, že „zdroj“ je jen v YAML — ve skutečnosti stačí export `SOURCE_URI` a YAML se přepíše.

---

## 3. Kód — křehká místa (Fáze 3)

### [run_core.py](../services/ai_core/run_core.py)

- **`_event_queue` max 512** — při `queue.Full` se události zahazují (`event_queue_full_drop`); ztráta dat při burst.
- **`init_db()` selhání** — DB writer běží, ale inserty se přeskakují (`_db_enabled` False).
- **Fallback `_dummy_loop`** — při nedostupném GStreamer se generuje syntetický rastr; MJPEG a detekce „něco dělají“, ale není to reálný vstup.
- **Telemetry** — `fps` / `inference_latency_ms` mohou zůstat `None` pokud `GstVisionPipeline` ještě neplní metriky; stav pipeline je ale publikován.

### [gst_pipeline.py](../services/ai_core/pipeline/gst_pipeline.py)

- **Opakované HTTP 403** — po **5** chybách splňujících `_is_forbidden_http_error` přechod do `FAILED` bez další recovery ([řádky ~454–468](../services/ai_core/pipeline/gst_pipeline.py)); užitečné pro CDN/YouTube, nutné znát při volbě zdroje.
- **Recovery** — `_start_recovery` nespustí druhé vlákno, pokud první recovery thread žije (guard).
- **EOS** — spouští recovery (konec souboru / odpojení).
- **RTSP** — před rebuildem `rtsp_describe_ok` v recovery smyčce; backoff až ~30 s.

### [redis_pub.py](../services/ai_core/ipc/redis_pub.py)

- **Pub/sub `config:updates`** — model a `recording_policy`; **zdroj** se mění přes **polling** `config:source` (`listen_source_changes`), ne přes stejný kanál — konzistentní s tím, že web zapisuje `config:source` přímo ([app.py](../services/web/app.py)).
- **Redis chyby u XADD** — logovány jako warning, pipeline nepadá.

---

## 4. Bezpečnost a provoz (Fáze 4)

| Riziko | Popis |
|--------|--------|
| **Bez autentizace API** | REST i WebSocket jsou bez API klíče / JWT — v LAN laboratoř OK, na veřejné IP **nepřijatelné** bez reverse proxy s auth nebo VPN. |
| **Snímky** | `GET /api/v1/snapshots/{name}` — kontrola pod `SNAPSHOT_DIR` brání path traversal; bez auth lze zkoušet známá jména souborů. |
| **SPOF** | Jeden proces `ai_core`; výpadek Redisu → web 503 / prázdná telemetrie; centrální bod selhání pro IPC. |
| **Watchdog** | [scripts/watchdog.sh](../scripts/watchdog.sh) jen loguje stale heartbeat — restart řeší restart policy kontejneru, ne skript. |

---

## 5. Kvalita a regrese (Fáze 5)

- **Automatizované testy:** v repozitáři nebyly nalezeny soubory `test*.py` — žádná CI pojistka proti regresím.
- **Linter / typechecker:** v projektu chybí konfigurace `ruff` / `mypy` (závisí na ruční disciplíně a review).

---

## Shrnutí priorit (co opravit / doplnit jako první)

1. **Provozní:** ověřit stack na cílovém HW podle sekce 1 a [terminal_diagnostics.txt](terminal_diagnostics.txt).  
2. **UX vstupu:** dokumentovat / zvýraznit vstup přes port 80 (již nápověda v UI).  
3. **Bezpečnost:** před vystavením mimo důvěryhodnou síť doplnit auth nebo síťové omezení.  
4. **Kvalita:** zvážit minimální pytest (API health, parsování diagnostických schémat) a statickou analýzu.

Tento dokument je živý výstup auditu podle plánu; po nasazení na Pi doplnit měřené JSONy z `/api/v1/diagnostics` do sekce 1.
