---
name: Tradier provider boundary
description: Credential and evidence boundaries for Tradier market data and sandbox paper execution.
---

Use a separate Tradier production token for trusted real-time one-minute market data and a sandbox token/account for paper execution. Never label Tradier sandbox market data as real-time SIP. Treat sandbox history or gain/loss responses that are HTTP-success but null-shaped as absent collections, not evidence.

**Why:** Tradier sandbox market data is delayed. Its account-history endpoint is unavailable for sandbox accounts, while its order list covers only the current market session; the sandbox can still return a null-shaped success response, so it cannot provide the complete historical broker evidence required by this project's reconciliation policy.

**How to apply:** Keep production market-data and sandbox trading URLs immutable and credentials separate. Preserve the fail-closed paper-ledger block unless complete broker evidence is established without assuming missing history is empty.