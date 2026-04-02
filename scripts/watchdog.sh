#!/bin/sh
# Monitors Redis ai:heartbeat; logs stale heartbeats (container restart policy handles crashes).
set -e
REDIS_URL="${REDIS_URL:-redis://redis:6379/0}"
STALE_SEC="${STALE_SEC:-25}"
SLEEP_SEC="${SLEEP_SEC:-10}"

echo "watchdog: redis=$REDIS_URL stale_after=${STALE_SEC}s"

while true; do
  ts="$(redis-cli -u "$REDIS_URL" GET ai:heartbeat 2>/dev/null || true)"
  if [ -z "$ts" ]; then
    echo "watchdog: no heartbeat key"
  else
    now="$(date +%s)"
    age="$(awk -v n="$now" -v t="$ts" 'BEGIN{print int(n-t)}' 2>/dev/null || echo 999)"
    if [ "$age" -gt "$STALE_SEC" ]; then
      echo "watchdog: stale heartbeat age=${age}s — check ai_core"
    else
      echo "watchdog: ok age=${age}s"
    fi
  fi
  sleep "$SLEEP_SEC"
done
