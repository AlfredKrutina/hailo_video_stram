# Nasazení a smoke testy (Docker)

## Porty: `:80` vs `:8080`

| Přístup | Kdo naslouchá | Poznámka |
|--------|----------------|----------|
| **`http://<host>/`** | **Nginx :80** → proxy na `web:8080` | **Doporučený** vstup pro uživatele i SPA: stejný origin pro HTTP i WebSocket (`/ws/...`). |
| **`http://<host>:8080/`** | Přímo **uvicorn** v kontejneru `web` | Obchází nginx — jiný origin než `:80` → riziko problémů s cookies, WS a CORS při mixování URL. |

Pro konzistentní chování používejte v prohlížeči vždy **`http://<IP>/`** (ne `:8080`), pokud nemáte výjimečný důvod ladit backend bez nginx.

## Minimální diagnostika (host)

```bash
cd docker
docker compose ps
docker compose logs -f --tail=200 web nginx ai_core
```

Z hosta (s mapováním portu 80 na nginx):

```bash
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1/health
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1/
```

Přímý kontakt s `web` uvnitř sítě (volitelné):

```bash
docker compose exec web python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read())"
```

## Automatický smoke skript

Z kořene repozitáře (vyžaduje běžící stack):

```bash
bash scripts/smoke_stack.sh
```

Proměnné `SMOKE_BASE` (výchozí `http://127.0.0.1`) a `SMOKE_WEB_DIRECT` (výchozí `http://127.0.0.1:8080`) upravují cíle curl.

## Checklist „proč zase 502“ na `/`

1. `docker compose ps` — služba `web` je **Up** a **healthy**?
2. Log `web`: traceback při importu / startup?
3. Testujte **`curl http://127.0.0.1/health`** přes **:80** (nginx), ne jen :8080, pokud chcete stejnou cestu jako uživatel.
4. Nginx `depends_on` čeká na **healthy** `web` — při pádu aplikace uvnitř `web` healthcheck selže a nginx se nespustí dříve, než je API dostupné (po `start_period`).
