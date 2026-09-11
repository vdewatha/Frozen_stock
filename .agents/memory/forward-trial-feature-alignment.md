---
name: Forward-trial feature alignment
description: Controlled paper evaluations require exact alignment between daily features and the intended decision session.
---

Daily inference features must come from the exact completed regular session being evaluated. When the matching row is delayed, leave the session decision open for a later worker retry; never consume its unique key with stale data.

**Why:** Allowing a prior-session row to support a current-session decision shifts the frozen model signal and invalidates the controlled forward evaluation.

**How to apply:** For any daily-cadence trial, require the feature date to equal the intended completed session date. Missing, stale, insufficient, or future feature rows create no decision and receive no coverage credit; retry after the matching observation arrives.