---
name: Broker activity timestamps
description: Alpaca journal activities may omit transaction_time, so reconciliation must use a broker-provided stable timestamp before observation time.
---

For broker activity immutability checks, compare the raw broker payload as the
source of truth and derive occurred_at from transaction_time or created_at
before falling back to the reconciliation observation time.

**Why:** Using the observation time for an activity without transaction_time
makes an unchanged journal credit appear to mutate on every later reconcile,
causing a false halt.

**How to apply:** Treat occurred_at as derived metadata that may be corrected
when the raw payload matches; never rewrite a differing raw payload silently.

Observation-time fallback is not acceptable provider qualification evidence.
Date-only non-trade activities must remain unknown for a precise-time contract;
do not infer midnight, timezone, or time from an activity identifier.

**Why:** Stable replay can conceal missing broker precision, and Alpaca's
documented non-trade shape may supply only a calendar date.

**How to apply:** Distinguish operational derived metadata from qualifying
broker evidence. Require explicit timezone-qualified broker time for the latter,
without treating repeatability as precision.