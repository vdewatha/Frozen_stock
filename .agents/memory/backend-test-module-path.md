---
name: Backend test module path
description: Module-path requirement for running the API server's Python tests from the monorepo workspace.
---

Run backend pytest with the backend directory on `PYTHONPATH`; otherwise tests may be collected from the workspace root without resolving the `app` package.

**Why:** The backend tests import `app` as a top-level package, while the workspace-level test invocation does not automatically add the backend directory to Python's import path.

**How to apply:** Use `cd artifacts/api-server/backend && PYTHONPATH=. pytest ...` for targeted or full backend validation.

Fake-broker recovery tests must also isolate the configured broker-account binding inside the test process. Never alter project credentials to make fake-account fixtures pass.

**Why:** These tests can inherit a real account binding from the workspace environment and fail during fake baseline initialization, before reaching the recovery behavior under test.

**How to apply:** Mock the binding for the fixture's lifetime and restore it afterward; continue using isolated test databases and fake gateways.