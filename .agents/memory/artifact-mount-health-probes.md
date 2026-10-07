---
name: Artifact mount health probes
description: Liveness checks for API services mounted under an artifact path such as /api.
---

**Rule:** Keep a dependency-free, unauthenticated liveness response at both the gateway root and the API artifact's mounted service path (`/api`).

**Why:** Replit Autoscale startup logs showed the API artifact being checked at `/api`; a response only at `/` did not satisfy that check, and `/api` otherwise entered the Clerk/API proxy path.

**How to apply:** When changing artifact paths or gateway middleware order, verify both `/` and `/api` return HTTP 200 before broker workers or FastAPI dependencies are involved. Keep `/api/health` for API liveness and `/api/ready` for dependency-backed readiness.
