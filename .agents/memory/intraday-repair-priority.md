---
name: Intraday repair priority
description: The ordering and readiness rule for recovering interrupted one-minute market-data imports.
---

Intraday recovery must refresh the newest completed current-session slice before older bounded backfill. A retry may therefore advance live data while remaining incomplete because historical gaps are still unresolved.

**Why:** Repairing an old gap first can leave the live feed stale, while treating the current slice as sufficient can create a false ready state.

**How to apply:** Keep each bounded slice independently durable, cap each poll to the configured repair windows, and assert both newest-slice priority and fail-closed readiness in interrupted-worker tests.