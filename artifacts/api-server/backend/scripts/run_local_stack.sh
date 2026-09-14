#!/usr/bin/env bash
set -euo pipefail

export REDIS_URL="redis://127.0.0.1:6379/0"
export ALLOW_LIVE_TRADING="false"

pids=()
cleanup() {
  trap - EXIT INT TERM
  for pid in "${pids[@]:-}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if ! redis_server="$(command -v redis-server)"; then
  echo "Cannot start the local stack: redis-server is required for ephemeral Redis." >&2
  exit 1
fi

"$redis_server" \
  --bind 127.0.0.1 \
  --port 6379 \
  --save "" \
  --appendonly no \
  --daemonize no &
pids+=("$!")

python3.11 - <<'PY'
import os
import time

import redis

client = redis.Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=1, socket_timeout=1)
for _ in range(50):
    try:
        if client.ping():
            break
    except redis.RedisError:
        pass
    time.sleep(0.1)
else:
    raise SystemExit("Local ephemeral Redis did not become ready")
PY

python3.11 -m alembic upgrade head

PYTHONPATH=. python3.11 scripts/run_stock_watchdog.py &
pids+=("$!")

# Keep the one-minute poll independent from daily imports and feature jobs.
# This launcher is used by both the local development workflow and the API
# artifact's production run command, so both environments use the same queue
# split and exactly one worker for each path.
python3.11 -m celery -A app.tasks.celery_app:celery_app worker \
  --loglevel=INFO \
  --hostname=intraday@%h \
  --queues=intraday_market_data \
  --concurrency=1 &
pids+=("$!")

python3.11 -m celery -A app.tasks.celery_app:celery_app worker \
  --loglevel=INFO \
  --hostname=general@%h \
  --queues=default,market_data,learning,paper_trading,risk \
  --concurrency=2 &
pids+=("$!")

python3.11 scripts/run_beat_with_lease.py &
pids+=("$!")

python3.11 -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:?PORT environment variable is required}" &
pids+=("$!")

wait -n "${pids[@]}"
echo "A local stack process exited; stopping the remaining processes." >&2
exit 1