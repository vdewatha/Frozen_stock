---
name: Alembic index parity
description: Keep ORM uniqueness declarations identical in kind to existing migration indexes so startup schema-drift checks remain clean.
---

When a migration creates a unique index, declare the matching SQLAlchemy metadata as an `Index(unique=True)`, not a `UniqueConstraint`.

**Why:** The API performs an Alembic metadata drift check at startup; PostgreSQL treats an equivalent unique constraint and unique index as different schema objects, so a kind mismatch prevents the service from starting.

**How to apply:** For every new uniqueness rule, match the migration operation and ORM declaration exactly, then run `alembic check` against the configured database before restarting the API.