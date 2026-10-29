# Phase 2: paper trade scorecard

Branch `phase2/exploration-scorecard`. This change only adds read-only analytics. It makes no broker calls and places no orders, and it leaves trading, risk, recovery and the autotrader exactly as they were.

## Why

Paper trading starts producing real trades on 2026-10-29. Two kinds of order are being placed:

- `paper_exploration`: learning probes from candidate and research strategies. These never count as promotion evidence.
- `scheduled_paper_signal`: orders from active strategies.

The owner's goal is for 8 of every 10 trades to be profitable while the account makes money overall. Until now nothing reported what the bot actually traded against that goal, so neither the owner nor the learning loop could see progress.

## What was added

### `app/services/paper_trade_scorecard.py`

**Rebuilding round trips** from `StockPaperOrder` and `StockPaperFill`:

- Each buy order opens one lot. Partial entry fills are folded into that lot. A lot counts as closed only once its whole entry quantity has been sold.
- Each sell fill closes lots FIFO, in this order of preference:
  1. A broker bracket leg (`leg_role` + `parent_order_id`) closes its own parent entry first.
  2. A sell that carries a strategy closes that strategy's lots for the symbol.
  3. An app-side sell with no strategy (sells do not store `strategy_id`) gets its strategy back by recomputing the autotrader's deterministic `client_order_id`. The key format is `{scheduled|explore}-paper:{ET date}:{symbol}:{strategy_type}:sell`. The same check against `...:protective_stop:sell` identifies the app protective stop. Protective stops close unbracketed lots first.
  4. Anything left over is matched FIFO by symbol. Quantity that still cannot be matched is listed under `data_quality.unmatched_exit_fills`.

**Exit reasons:**

| Reason | Meaning |
|---|---|
| `take_profit_leg` | Broker take-profit leg filled |
| `stop_loss_leg` | Broker stop-loss leg filled |
| `protective_stop` | App-side protective stop |
| `sell_signal` | The strategy's own sell signal |
| `manual` | Manual close or reduce |
| `recovery` | Recovery flatten |
| `other` | Anything else |

When one trade exits through more than one route, the reason that sold the most shares is reported, and the full split is kept in `exit_reasons`.

**Each trade records:**

- gross P&L, recorded fees (entry fees spread over the shares sold, exit fees spread over the lots each fill closed), net P&L and net return
- holding time
- entry time-of-day bucket in New York time: open, morning, midday, afternoon, close, extended
- source: `exploration`, `scheduled`, `forward_trial`, `manual`, `recovery` or `other`
- planned stop fraction from the bracket `stop_price`
- signal probability
- equity at entry

**Statistics** come from `summarize()`: trades, wins, losses, win rate (net), average win, average loss, payoff ratio, expectancy per trade (in dollars and as a return), profit factor, total P&L, fees and average holding time. When there are at least 20 trades it also adds a 95% bootstrap confidence interval for win rate and expectancy. The bootstrap uses 2000 resamples with a fixed seed, so results are reproducible.

The statistics are broken down by:

- day (exit date in New York time)
- strategy (name, status, sources)
- symbol
- source
- exit reason
- entry time bucket

**What the headline counts.** The headline, the goal and every breakdown except `by_source` count bot trades only, meaning `scheduled` plus `exploration`. `by_source` also shows manual and forward-trial round trips. Forward-trial orders use the `manual_control_room` source with a `trial_id`, so they are labelled `forward_trial` instead of `manual`.

**Goal progress:**

- wins and P&L over the last 10 trades
- `met_last_window`, which needs at least 8 of 10 trades profitable and the 10 together profitable
- the gap to 80%
- rolling win rate, expectancy and profit factor over the last 20, 50 and 100 trades. Each window carries a `complete` flag that stays false until there are enough trades to fill it.

**Open positions** are marked to market separately using the last reconciled broker position price, and are kept out of every statistic. Partially exited lots show the realized part of their P&L.

**SPY buy-and-hold** over the same window comes from stored `market_prices` daily closes, with the adjusted close used when present. No data fetch is made. The SPY return is compared with realized bot P&L divided by equity at the start of the window. If no SPY closes cover the window, the result is "unavailable" rather than a guess.

**Mistake labels** are attached to every losing trade through the new `trade_mistakes.classify_round_trip()`. This adapts the existing pure classifier, which until now only handled forward-trial lots:

- stop exits (the stop leg or the protective stop) map to `stop`
- sell-signal exits map to `horizon`
- planned risk is the position's notional times the stop fraction, divided by equity, so `oversized` means the loss went well past the planned stop
- excursions and the return after exit come from stored 1-minute bars when they exist

The labels are aggregated as primary counts, all counts, net P&L per primary mistake, and per strategy through `aggregate_mistakes`. They are hindsight labels and proposals only; `auto_apply` is always false.

**Markdown** is produced by `render_markdown()`.

### Exposure

- **API:** `GET /api/stock-paper/scorecard?days=N`, where N is 1 to 3650 and defaults to 30. It needs the **viewer** role because `/stock-paper/scorecard` was added to `READ_PATHS` in `app/core/security.py`. It computes the scorecard live, writes nothing, and adds a `history` list built from stored snapshots. Any other method on the path falls through to the admin default.
- **Daily snapshot:** a Celery beat entry, `paper-trade-scorecard-snapshot`, runs `app.tasks.jobs.paper_trade_scorecard_snapshot_job` at `crontab(minute=20, hour=16, day_of_week="mon-fri")`. That is wall-clock time in the app's America/New_York timezone. It runs on the `learning` queue and expires after 6 hours. The job goes through `_run_job`, so it takes the advisory lock, and it stores at most one snapshot per New York date.
- **Snapshot storage, with no migration:** each snapshot is one row in the existing `stock_paper_ledger_events` JSON table, with `account_id = NULL`, `event_type = "paper_scorecard_snapshot"` and `status = "recorded"`. I checked every reader of that table (reconciliation, transport recovery, cash policy, probe review, performance, broker status). They all filter on both `account_id` and a specific `event_type`, so a snapshot row can never be read as reconciliation, recovery or probe evidence. The stored payload is the scorecard with its trade and open-position lists cut to 20 entries each.
- **CLI:** `scripts/report_paper_scorecard.py [--days N] [--json] [--no-mistakes] [--record-snapshot]` reads the configured database and prints Markdown, or JSON with `--json`. It writes nothing unless `--record-snapshot` is passed.

### Control room

There is a new **Portfolio → Scorecard** tab at `/portfolio/scorecard`, built by `src/components/paper-scorecard-panel.tsx` with `getStockPaperScorecard()` and the `PaperScorecard*` types in `src/lib/api.ts`. It shows:

- a 7, 30 or 90-day window switch
- win rate against the 80% goal, with a progress bar and the confidence interval once one exists
- expectancy, payoff ratio and profit factor
- closed trades and the last-10 count
- rolling 20, 50 and 100-trade figures
- SPY compared with the bot
- open mark-to-market
- tables by strategy (exploration tagged) and by exit reason
- mistake chips
- the 15 most recent trades

It is read-only and has no action buttons.

## Tests

- `tests/test_paper_trade_scorecard.py` has 15 tests, all passing. They cover:
  - a synthetic book with a scheduled bracket (partial entry fills, take-profit leg, fees), an exploration bracket (stop leg in two partial fills), two strategies in one symbol (a sell signal with no strategy, recovered from its client id, plus a protective stop), an open lot marked to market, a trade outside the window and a manual round trip
  - every aggregate and the benchmark
  - that building the scorecard writes nothing
  - a partial exit staying open
  - no account at all
  - one snapshot per New York date and the history read
  - the CLI writing nothing
  - the bootstrap appearing at 20 trades and not at 19
  - goal and rolling windows, including 8 of 10 profitable trades that lose money overall
  - source labels and time buckets
  - the mistake adapter
  - the API route's viewer role, 401 without credentials and 422 for `days=0`
  - the beat crontab and queue
  - the job running through `_run_job`
- Related backend suites (celery app, security, stock-paper ledger, report_paper_mistakes, learning-loop feedback, monitor and production security, delayed SIP, readiness scheduler) pass: 92 + 88 tests.
- Control room: `tsc --noEmit` passes, `vite build` passes (with `PORT` and `BASE_PATH` set, as the vite config requires), and all 34 `tsx --test` component tests pass, including 2 new ones in `paper-scorecard-panel.test.tsx`. To run these I temporarily linked the main checkout's `node_modules` into the worktree and removed the links afterwards.
- Full backend suite: 1698 passed, 32 skipped, 0 failed (206 subtests), in 6 min 30 s. That run started just before the last small change to the new module, which labels forward-trial orders `forward_trial`. The 15 scorecard tests were re-run after that change and pass.

## Deploy notes

- **No Alembic migration.** Snapshots use the existing `stock_paper_ledger_events` table, so the Replit dev database needs no schema change for this branch.
- **Beat must be restarted** to pick up the new `paper-trade-scorecard-snapshot` entry. It can be shed with `PAPER_BEAT_EXCLUDE=paper-trade-scorecard-snapshot`. A worker that consumes the `learning` queue must be running.
- The first snapshot is written at 16:20 ET on the first weekday after deploy. Before any trades close it shows zero trades, not an error.
- The SPY comparison needs SPY rows in `market_prices`. The existing daily import at 08:15 and 17:15 ET supplies them. Without those rows the benchmark reports "unavailable".

## Limits

- A sell order does not store its strategy. Matching the client id is exact for orders the autotrader reserves today. If the autotrader's idempotency key format changes, those sells fall back to symbol FIFO and their exit reason shows as `other`. The mapping lives in `_ExitAttribution`.
- Fees are the ones Alpaca reports, and paper commissions are usually zero, so net P&L excludes spread and slippage except where they are already in the fill price. `costs_known` is reported for each trade and in aggregate.
- Mistake labels that need excursions (bad entry, gave back gains, signal decay, premature stop) only fire when 1-minute bars for the trade's window are stored.
