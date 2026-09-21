#!/usr/bin/env bash
set -euo pipefail

# Replit Publish applies the managed production schema before this process
# starts. The Autoscale web service must remain stateless: long-running Celery
# workers, beat, watchdog, and ephemeral Redis belong in an always-on worker
# deployment, not in the request-driven API process.
export ALLOW_LIVE_TRADING="false"

exec python3.11 -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:?PORT environment variable is required}"