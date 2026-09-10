# Forward shadow evidence report

`GET /crypto/bindings/{binding_id}/evidence` reports only one exact registered
model run, portable specification hash, and manifest hash. It is read-only and
has no promotion, order submission, or qualification side effects.

The report validates the current registered specification and each decision's
stored data snapshot against candles actually available at its recorded
observation time. Non-backfilled means the observation occurred within five
minutes of bar close; setting a flag alone is insufficient. Future observations,
invalid hashes, corrupt prices, training overlap, and mismatched model versions
are excluded and counted. Registration and binding creation must precede each
accepted observation. Backfilled observations are counted separately.

For matured outcomes, the report verifies the exact horizon target candle,
its provenance hash, scoring timestamp, realized price-change label, and stored
Brier value. It then recomputes Brier score, clipped logarithmic loss and
directional accuracy at a 0.5 probability threshold. Invalid outcomes never enter
these metrics. These are **prediction metrics, not trading P&L**. Overlapping
horizons are correlated; this is not a statistical-significance certificate.

Duration is strictly the span between first and last accepted observations.
Leaving the process stopped for 100 days does not manufacture 100 observed days.
The report separately exposes distinct observation dates and missing hourly bars
within the observed span; a sparse span does not demonstrate continuous uptime.
Empty or unscored datasets produce null metrics, never fabricated defaults.

The existing qualification policy is included as context (at least 100 completed
paper trades and 84 observed paper days). A shadow forecast is not a completed
trade and its price-change label is not net profit. Missing requirements remain
explicit: qualifying observed paper ledger, net costs/profits, reconciled equity
and drawdowns, benchmark/regime evidence, independent leakage/feature parity
review, and separate human capital approval. Both qualification eligibility and
live authorization are always false, regardless of predictive performance.

Implementation: `app.services.shadow_evidence.shadow_evidence_report`. The
optional `as_of` parameter is an internal deterministic-test seam; the HTTP
endpoint always uses the real clock. Tests exercise actual training, registry,
collection, shadow inference and outcome scoring with only public transport
mocked. No executable model artifacts are loaded.
