---
name: Interim migration compatibility
description: Rules for upgrading populated stock-only legacy schemas through the current Alembic head.
---

Historical stock-training repair namespaces can contain the stock tables and data without the shared strategy or audit tables from the full application schema. Migrations that cross those domains must conditionally add foreign keys and audit-chain changes only when the referenced tables exist; never fabricate missing shared tables or discard legacy rows.

**Why:** The populated interim-schema upgrade is intentionally isolated and must reach the configured head without turning missing unrelated application tables into a destructive or blocking migration failure.

**How to apply:** Keep the normal full-schema constraints intact, make partial-schema operations no-ops where there is no corresponding data, and keep the repair regression assertion aligned with the current Alembic head.