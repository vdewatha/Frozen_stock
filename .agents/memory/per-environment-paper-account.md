---
name: Per-environment paper account
description: Why development binds its own Alpaca paper account via DEV_PAPER_* and how account hand-off works
---

Each database must be the sole owner of the paper broker account it reconciles. Development
binds its own Alpaca paper account through DEV_PAPER_ALPACA_API_KEY / _SECRET /
DEV_PAPER_BROKER_ACCOUNT_ID (honoured only when ENVIRONMENT=local); production keeps PAPER_*.

**Why:** In October 2026 dev and production shared one paper account. Production placed the
orders, dev imported them without lineage, so dev showed eight unattributed holdings and an
external-order halt. Replit secrets are shared across environments, so overwriting PAPER_*
for dev would have rebound production too.

**How to apply:** Never point two environments at one paper account. Moving a database to a
new account goes through the admin account-transition route, which archives (never resets)
the old ledger; a book of only broker-imported inventory may be handed off, but any locally
reserved open order/position or unlinked fill refuses. The old shared ledger stays archived
in dev with its halt intact. A watchdog "monitor heartbeat is stale" pause on the new account
is the separate stale-monitoring issue, not an account-binding problem.
