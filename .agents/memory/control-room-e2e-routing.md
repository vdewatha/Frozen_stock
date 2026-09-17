---
name: Control-room E2E routing
description: How browser tests reach the managed control-room frontend/API and assert localized timestamps.
---

Control-room browser tests should run through the artifact proxy so the frontend and `/api` routes resolve together; direct Vite ports only serve the SPA fallback. Derive expected `toLocaleString()` timestamps in the browser instead of hard-coding a locale.

**Why:** The managed workflow injects a frontend port while API routing is provided by the preview proxy, and browser locale formatting differs from server-side assumptions.

**How to apply:** Use the preview proxy URL for E2E runs, make mocked route globs include query strings when callers add pagination parameters, and compare displayed dates against a browser-evaluated `new Date(...).toLocaleString()` value.

Test research submission rendering with controlled browser responses and verify real queue/persistence behavior against an isolated backend database. Keep real managed-role authentication in the browser tests.

**Why:** The browser shares the development database and workers. A regression test must not become a real provider request or trading mutation when the permission check being tested breaks.

**How to apply:** Block unrelated browser writes. For a real permission-denial probe, use a schema-invalid body so bypassed authorization still cannot queue work; separately test valid-body denial and dispatch in isolation.