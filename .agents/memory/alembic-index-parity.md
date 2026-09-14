---
name: Alembic index parity
description: Keep ORM uniqueness declarations identical in kind to existing migration indexes so startup schema-drift checks remain clean.
---

When a migration creates a unique index, declare the matching SQLAlchemy metadata as an `Index(unique=True)`, not a `UniqueConstraint`.

**Why:** The API performs an Alembic metadata drift check at startup; PostgreSQL treats an equivalent unique constraint and unique index as different schema objects, so a kind mismatch prevents the service from starting.

**How to apply:** For every new uniqueness rule, match the migration operation and ORM declaration exactly, then run `alembic check` against the configured database before restarting the API.

If an applied migration is later found to be missing an index or other schema object, add a corrective follow-on migration instead of editing only the historical revision.

**Why:** Existing databases will not rerun a revision whose head is already recorded, so changing that file alone leaves startup drift until a new revision applies the repair.

**How to apply:** Keep the historical migration safe for fresh upgrades, then make the follow-on migration idempotently create the missing object and verify both fresh and already-migrated databases.