---
name: PostgreSQL concurrency test isolation
description: Isolate PostgreSQL integration tests with schema translation when metadata tables have no explicit schema.
---

PostgreSQL tests that create a temporary schema must use SQLAlchemy `schema_translate_map={None: schema}` for both DDL and sessions; a search path alone does not redirect `Base.metadata.create_all()`.

**Why:** SQLAlchemy resolves metadata with the default `public` schema during DDL, so search-path-only fixtures can silently create or reuse shared tables and make repeated concurrency tests collide.

**How to apply:** Create the schema with a separate admin connection, bind test sessions to an engine using schema translation, and drop the schema with `CASCADE` after disposing the test engine.