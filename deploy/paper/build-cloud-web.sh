#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root/artifacts/strategy-control-room"
PORT=8080 BASE_PATH=/ NODE_ENV=production \
  VITE_LOCAL_PAPER_AUTH=true VITE_LOCAL_AUTO_VIEW=true \
  node node_modules/vite/bin/vite.js build --config vite.config.ts
