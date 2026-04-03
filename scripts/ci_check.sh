#!/usr/bin/env bash
# Statická kontrola Pythonu + návod na smoke po `docker compose up`.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "ci_check: python -m compileall services shared"
python -m compileall -q services shared

echo "ci_check: OK (compileall)"
echo "Volitelný integrační krok: z adresáře docker spusťte stack a pak bash scripts/smoke_stack.sh"
