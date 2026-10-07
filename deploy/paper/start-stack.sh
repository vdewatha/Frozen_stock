#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
compose=(docker compose --env-file "$root/deploy/paper/paper.env" -f "$root/deploy/paper/compose.yaml")

# Keep the learning pool bounded and reproducible across rebuilds/restarts.
learning_replicas="${PAPER_LEARNING_REPLICAS:-3}"
learning_concurrency="${LEARNING_CONCURRENCY:-2}"
case "$learning_replicas" in
  ''|*[!0-9]*) echo "PAPER_LEARNING_REPLICAS must be a non-negative integer" >&2; exit 2 ;;
esac
case "$learning_concurrency" in
  ''|*[!0-9]*) echo "LEARNING_CONCURRENCY must be a non-negative integer" >&2; exit 2 ;;
esac
if (( learning_replicas < 1 || learning_concurrency < 1 )); then
  echo "PAPER_LEARNING_REPLICAS and LEARNING_CONCURRENCY must be at least 1" >&2
  exit 2
fi
export LEARNING_CONCURRENCY="$learning_concurrency"

exec "${compose[@]}" up -d --build \
  --scale "learning=${learning_replicas}" \
  backend learning execution intraday market-data monitoring research risk scheduler watchdog web
