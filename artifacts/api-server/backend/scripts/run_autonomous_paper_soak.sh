#!/usr/bin/env bash
set -euo pipefail

# Run a bounded, isolated paper soak.  The schema is retained by default so
# the JSON report and database lineage can be reviewed after the processes stop.
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

duration="${SOAK_DURATION_SECONDS:-120}"
redis_port="${SOAK_REDIS_PORT:-6380}"
api_port="${SOAK_API_PORT:-5050}"
schema="${SOAK_SCHEMA:-paper_soak_$(date -u +%Y%m%d%H%M%S)_$$}"
report="${SOAK_REPORT:-reports/autonomous-paper-soak-$(date -u +%Y%m%dT%H%M%SZ).json}"

case "$schema" in
  ''|*[!a-zA-Z0-9_]*)
    echo "SOAK_SCHEMA must contain only letters, numbers, and underscores." >&2
    exit 2
    ;;
esac
if [[ "${DATABASE_URL:-}" != postgresql://* && "${DATABASE_URL:-}" != postgresql+psycopg://* && "${DATABASE_URL:-}" != postgres://* ]]; then
  echo "A PostgreSQL DATABASE_URL is required; the soak never runs against SQLite." >&2
  exit 2
fi
if ! [[ "$duration" =~ ^[0-9]+$ ]] || (( duration < 30 )); then
  echo "SOAK_DURATION_SECONDS must be an integer of at least 30." >&2
  exit 2
fi

python3.11 - "$schema" <<'PY'
import sys
from sqlalchemy import create_engine, text
from app.core.config import settings

schema = sys.argv[1]
engine = create_engine(settings.database_url, pool_pre_ping=True)
with engine.begin() as connection:
    connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
engine.dispose()
PY

export PGOPTIONS="-csearch_path=${schema},public"
export PORT="$api_port"
export REDIS_PORT="$redis_port"
export REDIS_URL="redis://127.0.0.1:${redis_port}/0"
export ALLOW_LIVE_TRADING="false"
export CELERY_BEAT_SCHEDULE_FILE="/tmp/frozen-stock-celerybeat-schedule-${schema}"

stack_pid=""
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cleanup() {
  trap - EXIT INT TERM
  if [[ -n "$stack_pid" ]]; then
    kill -TERM "$stack_pid" 2>/dev/null || true
    wait "$stack_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

bash scripts/run_local_stack.sh &
stack_pid=$!

for _ in $(seq 1 60); do
  if curl --silent --fail --max-time 2 "http://127.0.0.1:${api_port}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if ! curl --silent --fail --max-time 2 "http://127.0.0.1:${api_port}/api/health" >/dev/null 2>&1; then
  echo "API did not become healthy before the soak started." >&2
  exit 1
fi

# This is the scheduled path, not a manual lifecycle action.  The task may
# correctly defer outside a verified regular session or when account/feed
# prerequisites are unavailable.
PYTHONPATH=. python3.11 - <<'PY' || true
from app.tasks.celery_app import celery_app

result = celery_app.send_task("app.tasks.jobs.scheduled_stock_challenger_retraining_job", queue="learning")
print(f"scheduled_retraining_task_id={result.id}")
PY

echo "soak_started_at=${started_at}"
echo "soak_schema=${schema}"
echo "soak_redis=${REDIS_URL}"
echo "soak_duration_seconds=${duration}"
sleep "$duration"

# Exercise each scheduled continuation once at the end of the bounded window.
# These are the real scheduled jobs; a deferred/blocked result is preserved and
# is not converted into a manual lifecycle action.
PYTHONPATH=. python3.11 - <<'PY' || true
from app.tasks.celery_app import celery_app
from app.db.session import SessionLocal
from app.models import StockPaperTrial

scheduled_tasks = [
    ("app.tasks.jobs.scheduled_stock_paper_trial_handoff_job", "learning"),
    ("app.tasks.jobs.stock_forward_trial_observe_job", "paper_trading"),
    ("app.tasks.jobs.stock_forward_trial_reconcile_job", "paper_trading"),
    ("app.tasks.jobs.scheduled_stock_paper_promotion_job", "learning"),
]
for task_name, queue in scheduled_tasks:
    result = celery_app.send_task(task_name, queue=queue)
    print(f"scheduled_followup_task={task_name} id={result.id}")

with SessionLocal() as db:
    trial_ids = [
        trial.id
        for trial in db.query(StockPaperTrial)
        .filter(StockPaperTrial.status != "completed")
        .order_by(StockPaperTrial.created_at, StockPaperTrial.id)
        .all()
    ]
for trial_id in trial_ids:
    result = celery_app.send_task(
        "app.tasks.jobs.stock_forward_trial_evaluate_job",
        args=[trial_id],
        queue="paper_trading",
    )
    print(f"scheduled_followup_task=app.tasks.jobs.stock_forward_trial_evaluate_job trial_id={trial_id} id={result.id}")
PY

interruption_args=()
if [[ -n "${SOAK_INTERRUPTION_RECORDS:-}" ]]; then
  while IFS= read -r record; do
    [[ -z "$record" ]] && continue
    interruption_args+=(--interruption "$record")
  done <<< "$SOAK_INTERRUPTION_RECORDS"
elif [[ -n "${SOAK_INTERRUPTION_ARGS:-}" ]]; then
  # Keep the original flag-string input for compatibility. Prefer
  # SOAK_INTERRUPTION_RECORDS when a reason contains spaces.
  read -r -a interruption_args <<< "$SOAK_INTERRUPTION_ARGS"
fi

PYTHONPATH=. python3.11 scripts/report_autonomous_paper_soak.py \
  --output "$report" \
  "${interruption_args[@]}"