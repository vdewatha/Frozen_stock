#!/usr/bin/env bash
set -euo pipefail

# Run the bounded load scenario: heavy research on the learning queue, broker
# reconciliation, minutely intraday imports and a worker kill, all at once,
# against local fakes.  Like the paper soak it uses a disposable PostgreSQL
# schema and its own non-persistent Redis.  It starts no beat, API or
# watchdog, and it refuses any database that is not on this machine.
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

duration="${LOAD_DURATION_SECONDS:-180}"
redis_port="${LOAD_REDIS_PORT:-6381}"
restart_delay="${LOAD_RESTART_DELAY_SECONDS:-2}"
schema="${LOAD_SCHEMA:-load_scenario_$(date -u +%Y%m%d%H%M%S)_$$}"
report="${LOAD_REPORT:-reports/load-scenario-$(date -u +%Y%m%dT%H%M%SZ).json}"
run_dir="${LOAD_RUN_DIR:-}"

case "$schema" in
  load_scenario_*) ;;
  *)
    echo "LOAD_SCHEMA must start with load_scenario_." >&2
    exit 2
    ;;
esac
case "$schema" in
  *[!a-zA-Z0-9_]*)
    echo "LOAD_SCHEMA must contain only letters, numbers, and underscores." >&2
    exit 2
    ;;
esac
if [[ "${DATABASE_URL:-}" != postgresql://* && "${DATABASE_URL:-}" != postgresql+psycopg://* && "${DATABASE_URL:-}" != postgres://* ]]; then
  echo "A PostgreSQL DATABASE_URL is required; the load scenario never runs against SQLite." >&2
  exit 2
fi
if ! [[ "$duration" =~ ^[0-9]+$ ]] || (( duration < 60 )); then
  echo "LOAD_DURATION_SECONDS must be an integer of at least 60." >&2
  exit 2
fi
# Load belongs on a local database only: never the deployed app's server,
# even in a schema of its own.
python3.11 - <<'PY'
import os
import sys
from sqlalchemy.engine import make_url

host = make_url(os.environ["DATABASE_URL"]).host
if host not in (None, "", "localhost", "127.0.0.1", "::1"):
    sys.exit("The load scenario runs only against a PostgreSQL server on this machine.")
PY

if ! redis_server="$(command -v redis-server)"; then
  echo "Cannot run the load scenario: redis-server is required for its private Redis." >&2
  exit 1
fi

export LOAD_SCENARIO="1"
export LOAD_SCENARIO_SCHEMA="$schema"
export LOAD_DURATION_SECONDS="$duration"
export LOAD_RESTART_DELAY_SECONDS="$restart_delay"
export PGOPTIONS="-csearch_path=${schema},public"
export REDIS_URL="redis://127.0.0.1:${redis_port}/0"
export ALLOW_LIVE_TRADING="false"
export ENVIRONMENT="local"
export ACTIVE_PAPER_BROKER="alpaca_paper"
export ACTIVE_MARKET_DATA_PROVIDER="alpaca_iex"
# One boot id for the whole run, as one gateway process gives production: a
# restarted worker meets the leases its killed predecessor left behind.
export PAPER_BOOT_ID="$schema"
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
# macOS starts pool processes with spawn, not fork, and Celery only prepares a
# spawned task process when this is set (without it every task of the prefork
# research worker fails before it starts).  Linux forks and does not need it.
[[ "$(uname -s)" == "Darwin" ]] && export FORKED_BY_MULTIPROCESSING=1

# No provider may be reached.  The tasks call fakes; these two measures make a
# mistake fail instead of leaving the machine: no credentials, and a proxy
# address nothing listens on.
for name in PAPER_ALPACA_API_KEY PAPER_ALPACA_API_SECRET LIVE_ALPACA_API_KEY LIVE_ALPACA_API_SECRET \
  DEV_PAPER_ALPACA_API_KEY DEV_PAPER_ALPACA_API_SECRET DEV_PAPER_BROKER_ACCOUNT_ID \
  SHORT_PAPER_ALPACA_API_KEY SHORT_PAPER_ALPACA_API_SECRET SHORT_PAPER_BROKER_ACCOUNT_ID \
  ALPACA_API_KEY ALPACA_API_SECRET TRADIER_MARKET_DATA_API_KEY TRADIER_API_KEY FRED_API_KEY \
  PAPER_BROKER_ACCOUNT_ID ALERT_WEBHOOK_URL ALERT_TELEGRAM_BOT_TOKEN ALERT_EMAIL_SMTP_PASSWORD; do
  export "$name="
done
for name in HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; do
  export "$name=http://127.0.0.1:9"
done
export NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost"

[[ -n "$run_dir" ]] || run_dir="$(mktemp -d "${TMPDIR:-/tmp}/load-scenario.XXXXXX")"
mkdir -p "$run_dir/pids" "$run_dir/logs" "$(dirname "$report")"

redis_pid=""
supervisors=()
schema_created="false"
cleanup() {
  trap - EXIT INT TERM
  for pid in "${supervisors[@]:-}"; do
    [[ -n "$pid" ]] && kill -TERM "$pid" 2>/dev/null || true
  done
  for pid in "${supervisors[@]:-}"; do
    [[ -n "$pid" ]] && wait "$pid" 2>/dev/null || true
  done
  if [[ -n "$redis_pid" ]]; then
    kill -TERM "$redis_pid" 2>/dev/null || true
    wait "$redis_pid" 2>/dev/null || true
  fi
  if [[ "$schema_created" == "true" && "${LOAD_KEEP_SCHEMA:-false}" != "true" ]]; then
    python3.11 - "$schema" <<'PY' || echo "Could not drop schema; remove it by hand." >&2
import sys
from sqlalchemy import create_engine, text
from app.core.config import settings

engine = create_engine(settings.database_url)
with engine.begin() as connection:
    connection.execute(text(f'DROP SCHEMA IF EXISTS "{sys.argv[1]}" CASCADE'))
engine.dispose()
PY
  fi
}
trap cleanup EXIT INT TERM

python3.11 - "$schema" <<'PY'
import sys
from sqlalchemy import create_engine, text
from app.core.config import settings

schema = sys.argv[1]
engine = create_engine(settings.database_url, pool_pre_ping=True)
with engine.begin() as connection:
    if connection.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = :name"), {"name": schema}).scalar():
        sys.exit(f"Schema {schema} already exists; the load scenario needs a new one.")
    connection.execute(text(f'CREATE SCHEMA "{schema}"'))
engine.dispose()
PY
schema_created="true"

"$redis_server" \
  --bind 127.0.0.1 \
  --port "$redis_port" \
  --save "" \
  --appendonly no \
  --daemonize no >"$run_dir/logs/redis.log" 2>&1 &
redis_pid="$!"
echo "$redis_pid" >"$run_dir/pids/redis.pid"

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
    raise SystemExit("The scenario's private Redis did not become ready")
if client.dbsize():
    raise SystemExit("The Redis on LOAD_REDIS_PORT is not empty; it is not the scenario's own")
PY

python3.11 -m alembic upgrade head >"$run_dir/logs/alembic.log" 2>&1
PYTHONPATH=. python3.11 -m scripts.load_scenario_driver seed --run-dir "$run_dir"

# The hosted (compact) worker layout of artifacts/api-server/src/production-server.ts:
# label, queues, pool, concurrency.  tests/test_load_scenario_report.py keeps
# the queue lists and pools equal to the production file.  paper-research is
# the research lane: a prefork pool whose one task process runs a single
# learning job and exits.
workers=(
  "paper-core|default,market_data,intraday_market_data,research_market_data,scalp_research,learning_fast,paper_trading,risk,monitoring|threads|2"
  "paper-research|learning|prefork|1"
  "paper-fast|paper_trading,risk,monitoring,learning_fast|threads|2"
  "paper-intraday|intraday_market_data|threads|1"
  "paper-broker|broker_reconciliation|threads|1"
)

# Restart a worker that exits, after the same pause the production supervisor uses.
supervise() {
  local label="$1" queues="$2" pool="$3" concurrency="$4"
  local pool_args=(--pool="$pool" --concurrency="$concurrency" --prefetch-multiplier=1)
  [[ "$pool" == "prefork" ]] && pool_args+=(--max-tasks-per-child=1)
  (
    child=""
    stop_child() {
      if [[ -n "$child" ]]; then
        # A prefork worker's task processes first, so none outlives the run.
        pkill -KILL -P "$child" 2>/dev/null || true
        kill -KILL "$child" 2>/dev/null || true
      fi
      exit 0
    }
    trap stop_child TERM INT
    while true; do
      PYTHONPATH=. python3.11 -m celery -A app.tasks.celery_app:celery_app worker \
        --loglevel=INFO \
        --hostname="${label}@%h" \
        --queues="$queues" \
        --include=scripts.load_scenario_tasks \
        "${pool_args[@]}" \
        >>"$run_dir/logs/${label}.log" 2>&1 &
      child="$!"
      echo "$child" >"$run_dir/pids/${label}.pid"
      status=0
      wait "$child" || status=$?
      child=""
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ${label} exited (status=${status}); restarting in ${restart_delay}s" \
        >>"$run_dir/logs/supervisor.log"
      sleep "$restart_delay" &
      wait "$!" || true
    done
  ) 2>>"$run_dir/logs/supervisor.log" &
  supervisors+=("$!")
}

layout="{"
for worker in "${workers[@]}"; do
  IFS='|' read -r label queues pool concurrency <<<"$worker"
  supervise "$label" "$queues" "$pool" "$concurrency"
  layout+="\"${label}\": {\"queues\": \"${queues}\", \"pool\": \"${pool}\", \"concurrency\": ${concurrency}},"
done
echo "${layout%,}}" >"$run_dir/layout.json"

# Every worker must be publishing its heartbeat before the clock starts.
python3.11 - "${#workers[@]}" <<'PY'
import os
import sys
import time

import redis

expected = int(sys.argv[1])
client = redis.Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=1, socket_timeout=1)
for _ in range(240):
    if len(list(client.scan_iter(match="celery:worker:heartbeat:*", count=100))) >= expected:
        break
    time.sleep(0.5)
else:
    raise SystemExit("The scenario workers did not all report a heartbeat within two minutes")
PY

echo "load_scenario_schema=${schema}"
echo "load_scenario_redis=${REDIS_URL}"
echo "load_scenario_run_dir=${run_dir}"
echo "load_scenario_duration_seconds=${duration}"

PYTHONPATH=. python3.11 -m scripts.load_scenario_driver run --run-dir "$run_dir"
python3.11 scripts/report_load_scenario.py --run-dir "$run_dir" --output "$report"
