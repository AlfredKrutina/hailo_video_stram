#!/usr/bin/env bash
# Smoke: GET /health a GET / přes nginx (:80) a volitelně přímý web (:8080).
set -euo pipefail

SMOKE_BASE="${SMOKE_BASE:-http://127.0.0.1}"
SMOKE_WEB_DIRECT="${SMOKE_WEB_DIRECT:-http://127.0.0.1:8080}"

code() {
  curl -sS -o /dev/null -w "%{http_code}" "$1" || echo "err"
}

echo "smoke: GET ${SMOKE_BASE}/health"
h1="$(code "${SMOKE_BASE}/health")"
echo "  -> ${h1}"
test "$h1" = "200"

echo "smoke: GET ${SMOKE_BASE}/"
h2="$(code "${SMOKE_BASE}/")"
echo "  -> ${h2}"
test "$h2" = "200"

echo "smoke: GET ${SMOKE_WEB_DIRECT}/health (direct web, optional parity)"
h3="$(code "${SMOKE_WEB_DIRECT}/health")"
echo "  -> ${h3}"
test "$h3" = "200"

echo "smoke_stack: OK"
