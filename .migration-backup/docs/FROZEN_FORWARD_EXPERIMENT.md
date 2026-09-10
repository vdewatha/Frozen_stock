# Frozen random-forest forward experiment

This is the next prospective research stage after `MODEL_DIAGNOSTICS.md`. The existing pinned models, original paper accounts, and hourly research-candidate training remain unchanged. The challenger is trained once, then frozen; continuously changing it would invalidate this comparison.

## Frozen design

- Four fresh arms: champion and fixed random forest, each under baseline and stress costs.
- Identical source dataset bytes, feature configuration, 24-hour horizon, account budgets and entry/exit rules. Only model family and the explicitly paired cost profile vary.
- RF: 120 trees, minimum leaf size 5, random seed 42, one fitting thread. Original development/calibration split with purged label boundaries; sigmoid calibration on separate past calibration data. No final-period selection or parameter search.
- Baseline: 0.8% fees + 0.1% slippage per side. Stress: 1% fees + 0.2% slippage per side.
- Each strategy starts with $10,000 simulated cash and a maximum $100 entry budget, no shorting or pyramiding. Entry probability >=0.60 and modeled expected net return >0; exit probability <=0.40 or the fixed horizon. These are frozen research settings, not recommended exchange fees or personal investment allocations.
- One 84-day window, starting at a future UTC hour. Its exact start/end, model manifest pins, policy, execution-code hash and immutable Docker image ID live in `experiment.json`.

## Evidence and decision rules

Each arm has a separate SQLite comparison ledger including ML, cash, buy-and-hold, trend and mean-reversion accounts. Predictions must be observed in the first five minutes after an hourly close. Orders only fill at a subsequent, consecutive observed close proxy. Missed hours remain gaps; there are no retrospective fills.

`evidence/forecasts.sqlite` records all four paired probabilities before a future label can mature. Its outcome is a cost-adjusted 24-hour return between the next hourly close and the close 24 hours after that. This future close-to-close target differs from the historical next-open label and from the strategy's actual simulated exit timing; it is a descriptive probability diagnostic, not fill P&L. The worker allows 26 hours after the experiment ends for pending labels to mature.

Daily review is for health, accounting and progress, not early model selection. At the frozen checkpoint, both challenger cost profiles must satisfy all recorded checks:

- At least 95% hourly observation coverage and exactly matched champion/challenger observation times and candle hashes.
- At least 100 completed simulated ML round trips.
- Positive realized net P&L and positive marked equity P&L after modeled entry/exit costs already incurred.
- Profit factor >=1.2 and maximum account drawdown <=10%, with no drawdown halt.
- Better marked equity P&L than champion, cash, buy-and-hold, trend and mean reversion.

Insufficient observations or trades is inconclusive, including a model that never passes the entry gate. Gates will not be lowered to manufacture trading activity. Open positions remain marked at the checkpoint; no artificial liquidation is inserted, and unrealized liquidation costs are not reserved. A `review_candidate` result requests further independent examination, not live readiness. Probability diagnostics do not change these frozen gates.

## Commands and operations

1. Train a fixed challenger from each pinned champion using `backend/scripts/train_frozen_challenger.py --model PATH --manifest-sha256 SHA --output NEW_CANDIDATE_PARENT`.
2. Build the isolated runtime: `docker build --network none --pull=false -f backend/Dockerfile.forward -t trading-forward-rf:20260910 backend`.
3. Freeze with `backend/scripts/freeze_forward_experiment.py --pins PINS_JSON --root NEW_EXPERIMENT_ROOT --start-at FUTURE_UTC_HOUR --runtime-image-id SHA256_IMAGE_ID`. Pins map the four arm names to `directory` and `manifest_sha256`.
4. Start with `backend/scripts/start_forward_experiment.py --root EXPERIMENT_ROOT --source-db .paper-venue/research.sqlite`.
5. Inspect via `docker logs --tail 10 trading-forward-rf-experiment` and `docker exec trading-forward-rf-experiment python scripts/report_forward_experiment.py --experiment /experiment`.

The worker has no network, broker credentials or Docker socket. Model artifacts and source database are mounted read-only; only the new evidence directory is writable. The image itself is pinned by digest. `unless-stopped` resumes after Docker returns unless manually stopped. After the fixed window and label-maturation period it becomes quiescent, and it never extends the window. Machine sleep or Docker downtime prevents collection and causes genuine coverage gaps. Daily monitoring flags issues but does not silently reset accounts or restart a manually stopped worker.

Reports are read-only by default; `--output PATH` publishes a timestamped/content-hashed JSON and Markdown snapshot outside evidence. They reconcile accounting to recorded fills, not independently authenticated market execution.

## Remaining live-pilot gate

No live pilot is authorized or enabled by this experiment. Even a passing research checkpoint still needs independently reviewed out-of-sample evidence, performance across market regimes, executable quote/spread and latency checks, exchange fee validation, order/position reconciliation, incident review, kill-switch and outage tests, and a separate explicit capital/exchange/loss-limit decision by the user. The existing qualification evaluator requires qualifying observed execution evidence that this close-proxy simulation does not provide. Existing credentials must never be inferred from paper balances.

If the checkpoint fails or is inconclusive, diagnose the recorded cause and propose a new, separately frozen experiment. Do not keep optimizing against the same evaluation window and call it untouched.

## Activated September 10, 2026

- Experiment root: `.paper-training/forward-rf-20260911`
- Experiment ID: `839a038a817d6e62fcf7632bd265b91244c656ddd547c189a826a71c8e5ae7c8`
- Frozen window: September 11, 2026 00:00 UTC through December 4, 2026 00:00 UTC (84 days). In New York: September 10 at 8 p.m. through December 3 at 7 p.m.
- Baseline challenger: `14cf6027101a4afcd3b643839aad994c1bca5aa346711a4eb343d37a82412ad8`
- Stress challenger: `23a75252e03e6121b0df0a4d79f9f1fc9cede70128d5cef2e3219c388611c145`
- Exact runtime image: `sha256:092f91c41b169c1b49e1829f77dc9123ae5c1790dd8b7654054c4146ec2ce2e7`
- Initial verified worker status: `scheduled` at September 10 23:29 UTC. No forward observations were due yet.
- Existing daily review automation updated to inspect this study at 11 a.m. America/New_York alongside the original workers.
- Validation: 290 backend tests passed; independent reviewers checked trainer, dataset preservation, freeze contract, runner, evaluator and launcher.

The final selected challenger exports preserve the original pinned CSV bytes exactly. Two earlier reserialized exports remain preserved in the candidate parent directories, but are not in the experiment pins and are never used by this worker. No historical score comparison selected between these exports.
