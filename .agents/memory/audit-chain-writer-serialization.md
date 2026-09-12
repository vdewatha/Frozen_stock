---
name: Audit-chain writer serialization
description: Rules for preserving the PostgreSQL audit digest chain under concurrent and multi-event writes.
---

Audit writers must serialize on PostgreSQL and flush earlier audit rows in the
same transaction before selecting the previous digest. A hardening check that
finds a historical fork must report a breach and fail closed; it must not
rewrite or silently re-anchor persisted audit evidence.

**Why:** Multiple events created in one transaction can otherwise all link to
the last committed row, creating a fork even when each digest is individually
valid. Historical repair would destroy the evidence the audit chain is meant
to protect.

**How to apply:** Keep the advisory lock around audit writes, flush before
reading the previous digest, and require explicit disposable-database repair
or recovery procedures for an already-breached chain.