#!/bin/bash
set -e
pnpm install --frozen-lockfile

# The database schema is owned entirely by Alembic (artifacts/api-server/backend/alembic).
# Do NOT run `drizzle-kit push` here: lib/db has no Drizzle tables, so a push diffs the
# empty schema against the Alembic-managed database and prompts to drop every table.
(
  cd artifacts/api-server/backend
  PYTHONPATH=. python3.11 -m alembic upgrade head
)
