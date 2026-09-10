# Qualification evaluator

`app.services.qualification.evaluate_qualification` evaluates a mandatory
evidence dictionary against an immutable `QualificationPolicy`. The policy
fingerprint includes every threshold, so changing a threshold produces a new
identity. The result contains every gate and an explicit list of failures.
Absent values, invalid numeric values, missing provenance, overlapping time
periods, and failed operational checks block candidacy.

The default requires 100 completed observed paper trades over 84 days, positive
net expectancy after costs, profit factor >=1.2, drawdown <=10%, Brier score
<=0.25, better Sharpe ratio than the selected benchmark, two distinct regimes,
passed leakage/parity/reconciliation checks, and no manual, synthetic, backfilled
trades or critical incidents. Hashes bind the model, dataset and ledger snapshot.
Time fields must include offsets and preserve train/test/paper ordering.
Callers must supply `evaluated_at` from a trusted timezone-aware clock; future
observations are rejected against that explicit timestamp for reproducibility.

This is the deterministic WP14/WP19 decision component, not a completed
qualification monitor. A trusted ledger aggregator still must calculate and
persist observed evidence, bind those hashes to actual immutable records,
verify incident completeness, and periodically evaluate it. Direct callers can
fabricate dictionaries; this function is not an authentication boundary.
The regime list proves only claimed coverage, not positive performance within
each regime; regime-specific returns must come from the trusted aggregator.
`tests/test_qualification.py` contains the complete input schema example using
test-only observations; no example counts as real qualification evidence.

A passing result is only `live_pilot_candidate`, with `live_authorized=false`
and `requires_human_approval=true`. It neither changes a model status nor
submits an order. No live activation is reachable from this evaluator.
