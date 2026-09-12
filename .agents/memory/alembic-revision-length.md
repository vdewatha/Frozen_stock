---
name: Alembic revision length
description: The database's historical Alembic version column is varchar(32), so migration identifiers must remain within that limit.
---

Keep Alembic revision identifiers at 32 characters or fewer unless a dedicated
earlier migration widens the version column first.

**Why:** A longer revision can fail during the migration's version-row update
after its DDL has run, leaving the upgrade path unable to reach the new head.

**How to apply:** Check the identifier length whenever adding or renaming a
migration, and keep merge-point down revisions compatible with already-applied
heads.