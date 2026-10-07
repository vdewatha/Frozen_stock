---
name: Artifact mount health probes
description: Liveness checks for API services mounted under an artifact path such as /api.
---

**Rule:** Keep a dependency-free, unauthenticated liveness response at both the gateway root and the API artifact's mounted service path (`/api`). If the Python API child exits, inspect its startup traceback first: this supervisor also exits, so the gateway route cannot mask a backend startup failure.

**Why:** Replit Autoscale checks the API artifact at `/api`, but a later startup failure showed that a valid route does not help after FastAPI exits and the supervisor closes the gateway. Missing database columns caused the observed 500; Redis connection errors followed shutdown.

**How to apply:** When changing artifact paths or gateway middleware order, verify both `/` and `/api` return HTTP 200 before broker workers or FastAPI dependencies are involved. On publish failures, also inspect the configured startup path and Python startup logs. Keep `/api/health` for API liveness and `/api/ready` for dependency-backed readiness.
