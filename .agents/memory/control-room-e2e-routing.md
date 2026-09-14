---
name: Control-room E2E routing
description: How browser tests reach the managed control-room frontend/API and assert localized timestamps.
---

Control-room browser tests should run through the artifact proxy so the frontend and `/api` routes resolve together; direct Vite ports only serve the SPA fallback. Derive expected `toLocaleString()` timestamps in the browser instead of hard-coding a locale.

**Why:** The managed workflow injects a frontend port while API routing is provided by the preview proxy, and browser locale formatting differs from server-side assumptions.

**How to apply:** Use the preview proxy URL for E2E runs, make mocked route globs include query strings when callers add pagination parameters, and compare displayed dates against a browser-evaluated `new Date(...).toLocaleString()` value.