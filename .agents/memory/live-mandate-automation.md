---
name: Standing live mandate automation
description: How automatic live renewal/promotion/revocation stays inside a two-person mandate; rules future live work must keep.
---
Live authority may be renewed, promoted and minted automatically only inside the newest immutable two-person live mandate; any non-green gate lapses to the rollback target, and revocation triggers suspend the mandate.

**Why:** the user wanted live trading to continue without waiting on a person, but live money, broker ambiguity and still-blocked compliance mean two-person approval, bounded expiry and fail-closed gates cannot be removed — only moved up one level.

**How to apply:**
- Automation records itself only as executor; authority fields always carry the two mandate approvers.
- A human emergency stop or manual rollback of a mandate pilot must suspend the mandate, otherwise the scheduler would re-mint authority and undo the human action.
- Suspended mandates never self-heal; reinstatement is two-person and needs the emergency stop already reset.
- Cumulative loss is measured from the first snapshot after the ORIGINAL mandate in an amendment chain so amendments cannot reset it.
- Auto-graduation only for zero-blocker packages; graduation never sets live_authorized.
- Mandate tables are eagerly imported in app.models so the startup drift check sees them.
- Second approvers must be verified via signed identity whenever production identity is required (production AND approved-live), not only when environment == "production".
- Drawdown comes from reconciled equity snapshots; the live reconciler stores raw Alpaca /v2/account with no risk metrics, so never depend on precomputed metrics.
- Human stops must suspend the whole amendment chain (successors), since the pilot keeps the original mandate id.
