---
name: Unattended paper operation
description: Rules for standing mandates, explained-halt auto-resume, edge-decay re-admission and external alerts.
---
- A standing mandate only lets the scheduler mint each cycle's exact approval (cycle bounds, never mandate caps); dispatch rechecks stay unchanged.
  **Why:** approval projection/mismatch checks must keep comparing exact runtime bounds.
- Auto-resume is an allow-list of explained technical halt classes; anything else (residuals, uncertain orders, pagination, missing heartbeat, legacy pauses without recorded prior state) stays with a human.
  **Why:** fail-closed recovery; legacy pause payloads cannot prove the prior state was armed with kill switch off.
  **How to apply:** new pause sources must record prior_status/prior_kill_switch in the pause payload to be eligible.
- Not-evaluated re-admission retries land inside the next NYSE regular session, never a fixed 24h later.
- Re-admission counts only trained-but-unpromoted cycles as failures; cycles blocked before training retry without consuming an attempt.
- External alert sends happen only in the delivery job, never in the caller's transaction; stored errors are redacted. No alert secrets were requested — channels stay "unconfigured" until env settings are added.
- Mandate-minted approvals must hash exactly like operator approvals (no extra mandate fields); the order-time fence recomputes that digest.
- Mandate withdrawal (revoked/expired/pending) must be enforced on every not-yet-started trial start path, including operator retries.
