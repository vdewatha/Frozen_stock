#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
host="${ORACLE_PAPER_HOST:-129.80.219.66}"
port="${ORACLE_LOCAL_WEB_PORT:-18088}"
key="$root/.local/oracle/frozen-stock-paper"
socket="$root/.local/oracle/cloud-tunnel"

if ! ssh -S "$socket" -O check "ubuntu@$host" 2>/dev/null; then
  ssh -i "$key" -M -S "$socket" -f -N \
    -o ExitOnForwardFailure=yes -o ConnectTimeout=15 \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -L "127.0.0.1:$port:127.0.0.1:8088" "ubuntu@$host"
fi
curl --fail --silent --max-time 20 "http://127.0.0.1:$port/api/health"
printf '\nPrivate cloud dashboard: http://127.0.0.1:%s/\n' "$port"
