#!/bin/bash
set -e
pnpm install --frozen-lockfile
pnpm --filter db push

# API tables are maintained by Alembic rather than the Drizzle schema. Keep the
# development database current so Replit Publish can detect these changes.
(
  cd artifacts/api-server/backend
  PYTHONPATH=. python3.11 -m alembic upgrade head
)
