---
name: Post-merge schema ownership
description: Why the post-merge script must never run drizzle-kit push against this database.
---
Rule: the database schema is owned only by Alembic; the post-merge script runs `alembic upgrade head` and never `drizzle-kit push`.

**Why:** the Drizzle package has no tables, so a push diffs an empty schema against the Alembic-managed database and prompts to drop every table (orders, fills, accounts, research runs). With stdin closed the prompt failed, which was the only thing that prevented data loss (observed 2026-10-30).

**How to apply:** if someone adds Drizzle tables later, keep them in a separate schema or keep push out of automated scripts; never add `--force` to a push here.
