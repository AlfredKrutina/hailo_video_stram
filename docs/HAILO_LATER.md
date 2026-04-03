# Fáze 5 (odloženo): multi-stage Hailo / TAPPAS

Tento dokument fixuje rozsah z plánu přestavby: **plná multi-stage pipeline** (TAPPAS, `hailofilter` `.so`, druhá fáze HEF, složité propojení mezi stage) je **samostatný velký projekt**.

**Doporučení:** implementovat až když jsou stabilní:

- nginx → `web` (healthcheck, žádné pády při startu),
- `web` → bridge na `ai_core` video WS (best-effort, bez pádu procesu),
- `ai_core` s ONNX nebo jednoduchým `hailonet` a konzistentním GStreamer teardown.

V repozitáři zůstávají komentáře u proměnných `RPY_HAILO_*` v `.env.example` a v `docker/docker-compose.yml` jako orientační body pro pozdější rozšíření.
