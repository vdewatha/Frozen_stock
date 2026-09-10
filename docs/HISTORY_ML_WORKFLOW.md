# Historical-data recovery and ML paper activation

**September 6 update:** historical validation and both model-training runs are
complete; pinned ML paper inference is running. Nine provider-supported empty
hours are explicitly audited, not filled. See [GAP_REPAIR_20260906.md](GAP_REPAIR_20260906.md)
for the reviewed policy, actual model metrics and deployment evidence. The
original strict workflow description below is retained as implementation history.

## What changed

The Google Drive archive is no longer a prerequisite. A resumable collector
requests genuine BTC/USD trades from Kraken's public Trades endpoint for the
fixed interval January 1, 2025 through January 1, 2026 (exclusive). The first
3,000 real trades were collected and persisted before deployment. This is not
yet a completed year or a trained long-history model.

Source and request limits:
https://docs.kraken.com/api-reference/market-data/get-recent-trades . The API
returns at most 1,000 trades per request. A year requires thousands of calls;
completion takes hours and is not guaranteed by a deadline.

`trading-history-ml-workflow` was launched September 6, 2026. Its reviewed code:

1. Resumes the committed cursor, preserving raw responses and their hashes.
2. Validates trade identity, ordering, amounts and overlaps, and builds hourly
   OHLCV from trades. In-progress hour totals are provisional, not ready data.
3. Requires all 8,760 hours and end coverage, rebuilds aggregates from the raw
   pages, and verifies them before exporting immutable CSV and provenance.
4. Trains two separate calibrated v3 candidates at the fixed baseline and stress
   costs. An interrupted/failed training attempt requires review; it is not
   endlessly retried or selected using observed paper outcomes.
5. Pins model manifests and starts a new isolated comparison in its own database.
   Hashes, model topology, feature version, horizon, costs, full-year coverage and
   historical cutoffs are checked before prediction. No subsequent candidate
   automatically replaces these models.

An inference failure suppresses new ML entries while normal horizon and risk
exits remain available when fresh prices are present. The loader has numeric
safety bounds, but **no statistical out-of-distribution gate**. This experiment
is explicitly research-only and ineligible for qualification/live trading.

## Isolation and limits

State lives under ignored private `.paper-training/history-ml`. The original
paper database is mounted read-only as a single file; no provider directory,
credentials or Docker socket is mounted. The container runs non-root with a
read-only root filesystem, dropped capabilities, one CPU, 1 GiB memory and
bounded logs. Bridge networking is needed for public data and remains enabled
after comparison starts; this is not a network-none container.

Requests are paced at least one second apart, with bounded error backoff.
Raw response storage is capped at 2 GiB, the database at 8 GiB, and collection at
100,000 pages. Docker permits three failure restarts. A cap, missing hour,
integrity problem or exhausted retries requires review rather than fabricated
data or unlimited retries. Keep the computer and Docker running. An intentional
stop is not automatically undone.

Existing experiment, short-history trainer, and rule-only comparison are
preserved unchanged. They must not be pooled with this fresh experiment's
performance to hide losses or imply more independent evidence.

## Operations

Launch only if no workflow container exists:

```sh
docker build -f backend/Dockerfile.paper -t trading-paper-comparison:20260906-gaps backend
python backend/scripts/start_history_ml_workflow.py --database .paper-venue/research.sqlite --output .paper-training/history-ml --allow-verified-gaps
```

Read status without modifying data:

```sh
docker logs --tail 5 trading-history-ml-workflow
python backend/scripts/report_history_ml_workflow.py --output .paper-training/history-ml
```

Once comparison begins:

```sh
docker exec trading-history-ml-workflow python scripts/report_paper_comparison.py --database /research/comparison/baseline.sqlite
```

The existing 11 a.m. daily review now includes this workflow. It reviews progress
and results; it does not enable live funds, change thresholds or promote models.
