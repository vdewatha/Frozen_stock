# Running real-time paper experiment — 2026-09-04

The local experiment is **running**, using public Kraken BTC/USD hourly data.
No real money, exchange keys, leverage or shorts are configured. It is an
explicitly nonqualifying research trial, not a live-trading deployment.

## Current experiment

Update: the user requested continuous 24/7 operation with a daily 11 a.m.
America/New_York review, superseding the one-time stop. A separate candidate
trainer is described in [CONTINUOUS_TRAINING.md](CONTINUOUS_TRAINING.md).
The active paper model remains frozen; candidate training never promotes it.

- Model run: `13756b0d8209b08f39676aa5f45f1c2d3bbd2a4a7b537eda22499e0b3af68b65`.
- Training input:720 verified completed hourly Kraken candles;534 training rows,
  134 held-out rows, one-hour prediction horizon. Trained in isolated Linux.
- Logistic Brier0.252702; random forest0.265052; baseline0.249389 (lower is better).
  Neither model beats the baseline. Both were trained; only the frozen logistic
  numeric model is currently bound to forward paper observations.
- Binding1, paper approval1, version2 risk policy.
- Virtual starting cash:$10,000. Maximum entry notional and entry exposure:$100.
  Full owned-position exits can exceed the entry cap when prices rise; no shorts
  or added exposure are allowed. No pyramiding, no automatic model promotion.
- Conservative modeled fee:1% per side. This is deliberately not a claim about
  an actual exchange fee or a reconciled real-money wallet.
- Buy probability threshold0.6, sell threshold0.4; otherwise hold. No thresholds
  were lowered to force a trade. An initial late observation is backfilled and
  cannot trade. The next fresh hourly observation can be evaluated for a trial.

## Persistent state and services

State lives in the private ignored `.paper-venue/` directory:

- `research.sqlite`: new isolated database at migration0008; candles, model
  binding, trial policy, decisions, fills, provider exits and audits.
- `execution.dryrun.sqlite`: Freqtrade's separate simulated venue database.
- `provider.json`: private local API credentials; never print or commit it.

The original `backend/trading_app.db` and previous research database were not
modified. The new database was initially created as
`backend/paper_research_20260904.db`, then moved into the private state directory
before starting the runtime. There is only one active experiment database.

Docker containers:

- `trading-paper-kraken`: pinned Freqtrade2026.8, forced dry-run, localhost API.
- `trading-paper-research`: collection, shadow inference, recovery and bounded
  approved paper-trial cycle every60seconds. Uses the provider's network namespace.

Both have restart policies. Docker and the computer must remain running; sleep,
network loss or a Docker shutdown interrupts observations. The worker exits on
provider transport loss so a restart reconnects the provider namespace. Do not
run a second execution scheduler against the same account.

Read status without changing anything:

```sh
docker ps --filter name=trading-paper --format '{{.Names}} {{.Status}}'
docker exec trading-paper-research python scripts/report_paper_research.py --approval-id 1 --binding-id 1
docker logs --tail 5 trading-paper-research
```

To stop this experiment without deleting any data, stop the worker first, then
the provider. Also pause the corresponding scheduled monitor in the app:

```sh
docker stop trading-paper-research
docker stop trading-paper-kraken
```

Stopping is not deletion. Do not remove `.paper-venue/`, reset balances or overwrite
the provider database. Restart existing containers instead of rerunning initial
setup; never create a replacement account to hide an adverse result.

## What runs automatically

1. Collect and integrity-check completed public candles.
2. Produce one immutable, version-bound shadow decision per completed hour.
3. Recover existing submissions by exact provider evidence, without resending an
   uncertain order. A lost-response broker/gateway restart drill passed.
4. On fresh data and an armed approved trial, reserve one bounded order and send
   at most one dry-run request. Hold/stale/backfilled decisions cannot trade.
5. Reconcile fills and observed external provider exits. External exits are not
   relabelled as model decisions; they engage the kill switch for review.
6. Keep observed paper performance, including losses and conservative fees.
   A position is not counted as a completed trade before terminal evidence.

Collection failures still allow reconciliation, but prevent new trial orders.
Missing/ambiguous provider evidence preserves reservations and halts execution.
An engaged kill switch is never automatically cleared.

The app monitor `watch-real-time-paper-research` checks daily at 11 a.m. Eastern and
reports training health, completed trades, wins and losses, stale
data, service failures or a required operator decision. It is read-only and
does not alter risk or restart trading after a halt. Local scheduled monitoring
also requires the app and computer to remain running.

## Evidence and remaining limits

At startup:0 genuine forward decisions,0 completed paper trades,0 positive trades.
The initial startup snapshot is explicitly excluded as backfilled. Query the
report for current values; this document does not manufacture elapsed evidence.

Actual disposable Freqtrade dry-run tests completed a buy/sell round trip and a
lost-acknowledgement broker+gateway restart recovery. Those test decisions were
synthetic, in discarded isolated databases, and are **not** experiment results.

A positive paper trade may or may not occur. Even several positive trades do not
establish reliable profitability. Models remain frozen for this trial rather
than repeatedly tuning against the same observed outcomes until a win appears.
Additional models require separately versioned training/evaluation and an
explicit new paper approval. Real-money trading remains a separate, unapproved
stage requiring robust evidence and operational/capital authorization.

## Verification for this increment

197 backend tests passed. Independent reviewers accepted the evidence report,
bounded trial cycle, policyv2 full-position exits, external-exit accounting,
cross-approval performance attribution, launcher and data-failure recovery path.
Fresh PostgreSQL16 migrations through0008, schema parity, concurrent account
initialization, exact money storage and kill-switch persistence passed in a
disposable container that was removed afterward. Only the two intentional paper
runtime containers remain running.
