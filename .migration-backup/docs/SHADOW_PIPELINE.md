# Version-bound research shadow pipeline

This pipeline records research predictions, not orders or capital approval. Every
decision and scored outcome is permanently ineligible for qualification under the
current experimental model registry. Positive predictions are not evidence of a
profitable strategy.

## Prerequisites and workflow

1. Upgrade a backed-up database through migration `0006_shadow_pipeline`.
2. Collect uninterrupted, verified Kraken BTC/USD hourly candles through the
   durable collector. Inference refuses gaps, stale history, or future observations.
3. Train an hourly research run with its explicit instrument/timeframe binding.
   Register the immutable manifest and artifacts. Legacy daily research bundles
   cannot be rebound to hourly crypto.
4. Call `register_shadow_model(db, run_id, spec, actor)` with the exported
   `logistic_regression.json` dictionary. Commit the caller-owned transaction.
   The actor is an authenticated service/operator identifier, not a new user
   identity system. The function validates the exact portable artifact checksum,
   manifest, feature identity, training cutoff, instrument and observation horizon.
5. Call `run_shadow(db, binding_id)` after hourly collection; commit its result
   **including when it returns None**, so blocked audits persist. Do not expose
   caller-supplied clocks through production APIs. `as_of` is for deterministic
   tests and explicitly historical research.
6. Call `score_shadow(db)` after subsequent completed candles. It scores only once
   the exact horizon bar exists, storing the target candle checksum and outcome.

## Safety and semantics

- Only allowlisted numeric logistic JSON is interpreted. No pickle/joblib loading,
  imports from model metadata, or caller-provided executable code is supported.
- Features use only recorded candles available at the decision cutoff and the
  same versioned feature generator used in training.
- A binding plus completed bar is unique. Repeated calls return the original
  decision without modifying its observation timestamp or prediction.
- The policy maps probability >=0.6 to a research `buy` intent, <=0.4 to `sell`,
  otherwise `hold`. These labels have no execution authority.
- Predictions more than five minutes after bar close are marked backfilled.
  All predictions remain ineligible regardless of latency or eventual return.
- Stored decisions carry data/spec hashes, observation time, bar close, latency,
  reference price and probability. Model/manifest identity is retained on binding.
- Blocked inference produces a sanitized audit code, never a fabricated signal.
- Caller transactions and database unique constraints deduplicate concurrent
  writes using savepoints; losing inserts return the original immutable record.
  No network work occurs inside inference.

Independent review: the durable-data worker reviewed this package, requested
race-safe inserts and scoring-time identity revalidation, and accepted the fixes
after running all seven shadow tests. Production clocks remain server-owned.

Longer historical datasets, genuine forward observation, calibration, independent
strategy qualification and live execution authorization remain separate work.
