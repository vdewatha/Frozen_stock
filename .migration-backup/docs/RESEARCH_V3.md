# Calibrated research and isolated strategy comparisons

**Current deployment:** long-history models are trained and pinned paper
inference is active. [Gap repair and results](GAP_REPAIR_20260906.md) supersedes
the earlier acquisition/inactivity notes below. The original rule-only
comparison is preserved separately.

This increment is research-only. It does not replace the frozen experiment,
promote a model, authorize orders, or use real funds.

## Calibrated candidates

`backend/scripts/train_research_v3.py --history-bundle BUNDLE --output OUTPUT`
requires a hash-verified history bundle with at least 8760 contiguous hourly rows.
The v2 features and 24-hour cost-aware labels are retained. Chronological data is
split into 60% development, 20% calibration, and 20% final test. Label endpoints
are purged at partition boundaries. Development-only walk-forward folds choose
logistic regression or random forest; a sigmoid on base-model logits is fitted
only on calibration data. Final labels never select or fit the model.

Each immutable candidate includes numeric JSON inference, source/data/code
identity, file hashes, calibrated and uncalibrated metrics, a constant baseline,
reliability bins, and calibration-only payoff estimates. JSON inference is a
trusted-export helper, **not a general untrusted-artifact runtime loader**.
Repeated overlapping final tests are not independent evidence of improvement.

## Forward paper comparison

`backend/scripts/run_paper_comparison.py --source-db SOURCE --output OUTPUT`
reads collector data using SQLite read-only mode and writes only separate
`baseline.sqlite` and `stress.sqlite` files. `--once` performs one cycle.
No broker or exchange credentials are used. A process lock prevents duplicate
workers. Each configuration is frozen per database.

Five independent simulated accounts track trend, mean reversion, cost-aware ML,
cash, and fixed-budget buy-and-hold. The current worker deliberately supplies no
ML model: that account reports `model_unavailable` until a validated, separately
pinned inference integration is implemented. It must not invent probabilities
or silently use the original model.

Signals are decided on closed candles and simulated at the next *newly observed
hourly close*, not a retrospectively available open. Observations must arrive
within five minutes of bar close. Missed hours cancel queued orders. This coarse
execution proxy is not an exchange fill. Per-account starting cash is $10,000,
entry budget is $100, and no shorts, pyramiding, or leverage are allowed.
Decision reasons, pending orders, costs, balances, and drawdown are persisted.

Read-only inspection:
`backend/scripts/report_paper_comparison.py --database OUTPUT/baseline.sqlite`.

Deployment on September 6, 2026: `trading-paper-comparison` is running with no
network, no capabilities, a read-only root filesystem and source SQLite mount,
and only `.paper-training/comparison` writable. It restarts unless stopped.
Initial inspection at 04:51 UTC correctly rejected a late observation; no
comparison step or fill had yet occurred. It will wait for an on-time newly
closed bar. Existing three containers and daily review configuration are unchanged.

Baseline assumptions are 0.8% fee per side and 0.1% slippage; stress assumptions
are 1% and 0.2%. These are scenarios, not verified account-specific charges.
Fee source checked September 6, 2026:
https://www.kraken.com/features/fee-schedule . The original experiment's costs
are unchanged.

`execution_stress.execution_stress` separately evaluates fixed offline signals
under baseline costs, higher costs, extra latency, thin liquidity, and missed
fills. It never grants trading eligibility and does not reconstruct an order
book or queue position.

## Remaining gates

Update September 6: the archive quota has been worked around using a different
official data product, not by bypassing its download restriction. The resumable
public-trades workflow is now downloading 2025 data, with validated long-history
training and first pinned ML paper activation configured to follow completion.
See [HISTORY_ML_WORKFLOW.md](HISTORY_ML_WORKFLOW.md). The secure runtime loader
and optional pinned-model comparison integration are implemented. The earlier
rule-only worker still intentionally has no ML model. No completed full-year
training or active long-history ML inference is claimed while download continues.

The official historical archive returned Google Drive quota-exceeded HTML on
September 6, 2026. No historical CSV or longer-history trained model was obtained.
Acquire and validate the actual hourly history before running v3. Then review
held-out metrics and gather genuinely forward paper observations under the
fixed model/cost policy. Profitable trades alone do not establish an edge;
cost-stressed performance, drawdown, calibration, and sufficient independent
evidence must be evaluated before any separately authorized live pilot.
