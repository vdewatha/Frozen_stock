# Paper cost calibration, 2026-11-06

Branch `claude/magical-tharp-3e35af`, roadmap `self-evolution-roadmap-20261106.md`.
**Question:** every backtest, gate replay and experiment prices execution with a guess (5 bps floor + 5% of
the causal daily volatility + square-root impact, plus a 1 bp fee per side). The bot has months of real paper
fills with a recorded decision price. What do its trades actually cost, and how far is the guess off?

**Goal used:** profit after costs. Realised costs decide whether an edge survives, so the replays the bot
judges its experiments on should price execution at what the bot actually pays, not at the published guess.

## What was built

`app/services/paper_cost_calibration.py`, paper only; nothing here places, blocks or exits an order, and
strategies are never written.

1. **Measure.** Every `StockPaperFill` of the last 60 sessions (`paper_cost_calibration_lookback_sessions`)
   is joined to its order. The entry orders are limits reserved at the decision price
   (`StockPaperOrder.limit_price`), a take-profit leg carries its limit, a protective stop its stop price; a
   fill is scored per side in basis points with `trade_attribution.slippage_bps` (positive = worse than the
   decision price) plus the fee the broker reported. Each fill is bucketed by symbol, by order kind (`market`,
   `limit` entry, `bracket_take_profit`, `protective_stop`, `unknown` when no order is linked) and by
   time-of-day (`open`: first 30 minutes, `midday`, `close`: last 30 minutes, `off_hours`). Next to each fill
   sits what the published model would have charged for the same session: `triple_barrier.side_cost_bps`
   with volatility and average volume from the trusted daily bars strictly *before* that session
   (`backtester._liquidity`, the same arithmetic the backtester uses at an entry) and the fill's own
   quantity. The measurement table reports counts, mean, median and 90th percentile of realised slippage and
   of realised total cost, the mean modelled cost, the mean gap (realised total minus modelled total) and the
   residual `sum(realised slippage) / sum(modelled slippage)` for every bucket, plus the median of the
   per-fill residuals.
2. **Calibrate.** Per cell (symbol class × time bucket; classes are `index_etf` for SPY/QQQ/IWM/DIA,
   `sector_etf` for the SPDR sector ETFs and SMH, `single_stock` for the rest) the raw multiplier is the
   ratio of mean realised to mean modelled slippage (a ratio of means, so a few near-zero modelled values
   cannot blow it up). A cell needs 20 usable fills (`paper_cost_calibration_min_fills`; a fill is usable
   when it has a decision price) before it is fitted; the fit is shrunk toward 1.0 with 20 pseudo-fills,
   `m = (n * raw + 20) / (n + 20)`, then capped to [0.5, 3.0]. The same fit is made per class over the
   whole day, per bucket over all classes, and over everything, so a sparse cell falls back to a coarser one
   at lookup time. The result is one `paper_cost_calibration` audit row per completed session (no
   migration), carrying the measurement table, every cell, the fills it was fitted on (id, order, symbol,
   side, session, bucket, decision and fill price, realised and modelled bps; the last 2,000 are stored) and
   the date range.
3. **Apply.** `triple_barrier.side_cost_bps` gained a `multiplier` and `label_events` /
   `label_open_session` an optional `calibration: CostMultipliers(entry, exit)`; the backtester's
   `execution_cost_bps` honours a new `BacktestConfig.cost_multiplier` (and an explicit override). Defaults
   are 1.0, so every existing backtest, label and replay is unchanged. `CostCalibration.for_symbol` resolves
   one replayed symbol's multipliers: the replay's next-open entry takes the class's `open` cell, exits the
   class's whole-day cell, each falling back to the global ones. `gate_replay.simulate` reads the latest
   calibration by default (`gate_replay.cost_calibration`, one ledger read cached for five minutes per
   process; `calibration=None` replays with the published model), so the experiment engine and the gate
   cost audit replay with realised costs without a change to either.
4. **Job and surface.** `paper_cost_calibration_job` on the learning queue, Saturday 13:30 ET, before the
   weekly report (14:07) and the Sunday experiment run (12:00). `GET /stock-paper/cost-calibration` (viewer)
   returns the latest calibration, the measurement table and the cells in force. The weekly report gained a
   `cost_calibration` section: fills measured, realised vs modelled cost per side, the gap, the residual and
   the multipliers in force.
5. **Tests.** `tests/test_paper_cost_calibration.py` (11 tests): symbol classes and time buckets, order kinds
   and decision prices, one fill scored against its decision price and the model, the summary statistics,
   shrinkage and caps, cell fitting with fallbacks and the stored form, the cost functions with and without a
   calibration (triple barrier and backtester), the audit row and the views, the no-fills case, the loader
   (including a failing database) and the replay using the calibration, and the beat / queue / route wiring.
   Full backend suite (without `tests/test_scalp_research.py`): see the PR.

## First measured numbers

None yet. The local SQLite database has no paper fills, orders or daily bars, so the first measurement is
the one the Saturday job records in production. Until then every cell is unfitted, the stored calibration is
the identity and `gate_replay.cost_calibration` returns `None`: the replays keep the published model.

What to read in the first report (`GET /stock-paper/cost-calibration`):

- `tables.overall.residual` is the single number that says how far the guess is off (1.0 = the model is
  right on average; 2.0 = the bot pays twice the modelled slippage).
- `tables.by_order_kind`: a protective stop's slippage is measured against its stop price, so it shows the
  gap-through cost the replay's "fill at the level" assumption hides; a bracket take-profit should sit at
  or below zero (a limit cannot fill worse than its price).
- `tables.by_time_bucket`: the replay enters at the open; if the `open` bucket is dearer than `midday`, the
  entry multiplier will say so.
- `cells`: which cells reached 20 fills; the fallbacks make the first calibration mostly the global ratio.

## What is not measured yet

- The decision price of a limit entry is the price the cycle reserved at, not the quote at the moment the
  order reached the broker; latency between the decision and the submission is inside the measured slippage,
  which is the number that matters for the replay but not a pure execution cost.
- Alpaca paper fills carry no market impact, so the impact term of the model is calibrated against a venue
  that never charges it; the multiplier is honest about the paper venue, not about a live one.
- Fees are the broker-reported ones (zero on Alpaca paper); the model's 1 bp fee is left as published and
  reported alongside, not scaled.
- Unlinked fills (no `order_id`) are counted but not priced; a large `unknown` row in `by_order_kind` means
  the activity ledger is ahead of the order ledger.

## Decisions worth knowing

- The multiplier scales the whole modelled slippage (floor, volatility term and impact together), not the
  volatility fraction alone: with a handful of fills per cell a one-parameter fit is all the data supports.
- The fit is on slippage only; fees are known exactly and are added unscaled.
- The exit multiplier is the class's whole-day cell because exits (stops, targets, time exits) land anywhere
  in the session; the entry multiplier is the opening cell because the replay enters at the open. If the bot
  never fills in the first 30 minutes the opening cell stays unfitted and the entry falls back to the
  whole-day cell.
- `gate_replay.simulate` reads the calibration by default so the experiment engine (not edited, another
  session owns it) picks it up; an engine run therefore judges every setup, including the baseline, under the
  same realised costs, which keeps the pre-registered decision rule a like-for-like comparison.
