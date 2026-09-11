---
name: Equity feed entitlement
description: Records the authorization boundary for the selected real-time equity feed.
---

Use Alpaca Algo Trader Plus SIP completed one-minute regular-session bars with a 60-second late-trade allowance for personal research and paper trading. Do not purchase a subscription automatically. Credentials must be supplied through workspace secrets, and actual SIP entitlement must be verified against the authenticated account before treating the feed as available.

**Why:** The provider and published plan were selected, but the user did not authorize a purchase and account-level entitlement could not be verified without credentials.

**How to apply:** Keep ingestion and paper decisions fail-closed until secure credentials are configured and the provider confirms SIP access. Treat published pricing or configuration alone as insufficient evidence of entitlement.