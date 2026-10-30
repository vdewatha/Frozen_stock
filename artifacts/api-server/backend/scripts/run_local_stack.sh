#!/usr/bin/env bash
set -euo pipefail

redis_port="${REDIS_PORT:-6379}"
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:${redis_port}/0}"
export ALLOW_LIVE_TRADING="false"

pids=()
cleanup() {
  trap - EXIT INT TERM
  # Stop beat/workers/API before Redis so lease refreshers can exit cleanly.
  for ((index=${#pids[@]}-1; index>=0; index--)); do
    pid="${pids[index]}"
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
  --port "$redis_port" \
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

if [[ "${ENVIRONMENT:-local}" == "production" ]]; then
  # Replit Publish applies the development schema to the managed production
  # database before this process starts. Running Alembic here would replay DDL
  # against that already-current schema while its legacy revision marker still
  # reflects the last Alembic-managed release.
  echo "Using the publish-managed production schema; skipping Alembic upgrade."
else
  python3.11 -m alembic upgrade head
fi

if [[ "${PAPER_WORKERS_ENABLED:-true}" == "true" ]]; then
  PYTHONPATH=. python3.11 scripts/run_stock_watchdog.py &
  pids+=("$!")

  # PAPER_DIRECT_MONITOR_LOOP=true removes stock-monitoring-job from Celery
  # beat, so the monitor must run as its own supervised process (production
  # does this from the Node gateway). A crashed loop is relaunched here instead
  # of stopping the whole stack; the loop itself never mutates watchdog or
  # recovery state on failure, so recovery stays fail-closed meanwhile.
  if [[ "${PAPER_DIRECT_MONITOR_LOOP:-false}" == "true" ]]; then
    (
      monitor_pid=""
      stop_monitor() {
        if [[ -n "$monitor_pid" ]]; then
          kill -TERM "$monitor_pid" 2>/dev/null || true
          wait "$monitor_pid" 2>/dev/null || true
        fi
        exit 0
      }
      trap stop_monitor TERM INT
      while true; do
        PYTHONPATH=. python3.11 scripts/run_stock_monitor_loop.py &
        monitor_pid="$!"
        monitor_status=0
        wait "$monitor_pid" || monitor_status=$?
        monitor_pid=""
        echo "Direct stock monitor loop exited (status=${monitor_status}); restarting in 5s." >&2
        sleep 5 &
        wait "$!" || true
      done
    ) &
    pids+=("$!")
    general_queues="default,market_data,learning,paper_trading,risk"
  else
    # Beat owns monitoring and routes stock-monitoring-job to the monitoring
    # queue, so a local worker must consume it or no snapshots are written.
    echo "PAPER_DIRECT_MONITOR_LOOP is not true; Celery beat runs stock-monitoring-job."
    general_queues="default,market_data,learning,paper_trading,risk,monitoring"
  fi

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

  # Keep 1-minute scalp research independent from market-data and learning
  # backlogs so a slow experiment cannot starve its own evidence queue.
  python3.11 -m celery -A app.tasks.celery_app:celery_app worker \
    --loglevel=INFO \
    --hostname=scalp-research@%h \
    --queues=scalp_research \
    --concurrency=1 &
  pids+=("$!")

  python3.11 -m celery -A app.tasks.celery_app:celery_app worker \
    --loglevel=INFO \
    --hostname=general@%h \
    --queues="$general_queues" \
    --concurrency=2 &
  pids+=("$!")

  PYTHONPATH=. python3.11 scripts/run_beat_with_lease.py &
  pids+=("$!")
else
  echo "PAPER_WORKERS_ENABLED=false; skipping watchdog, Celery workers, and beat."
fi

python3.11 -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:?PORT environment variable is required}" &
pids+=("$!")

wait -n "${pids[@]}"
echo "A local stack process exited; stopping the remaining processes." >&2
exit 1
