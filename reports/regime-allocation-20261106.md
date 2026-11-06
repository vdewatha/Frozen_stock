# Regime-Aware Risk Budget, 2026-11-06

**Question:** the paper book weights each strategy's risk fraction by its recent net record, blind to the market
regime. Would conditioning that weight on the regime cell the BUY opens in (SPY trend × VIX band × breadth) raise
the book's expectancy per unit of risk without a worse drawdown?

**Decision rule (fixed before the run, in `scripts/run_regime_allocation_backtest.py`):** the regime-aware arm is
adopted only if, in every window (all, 2015+, 2020+):
* its notional-weighted per-trade net expectancy is not lower than *both* equal weights and the live
  performance-weighted arm;
* its book max drawdown is not worse than both; and
* it changed the size of at least one trade.

Otherwise `paper_allocation_regime_enabled` stays off.

## Answer: not adopted; the setting ships off

The regime-aware arm lowers the drawdown a little in every window but lowers the expectancy per unit of risk in two
of three, so it fails the rule. **`paper_allocation_regime_enabled` defaults to `False`.** The code, the per-regime
table and the backtest stay in place so the question can be re-asked on forward paper trades.

| Arm | Trades (all / 2015+ / 2020+) | Weighted exp. bps, all / 2015+ / 2020+ | Max DD, all / 2015+ / 2020+ | Total return, all / 2015+ / 2020+ |
|---|---|---|---|---|
| equal weights | 11,547 / 5,240 / 3,274 | +0.27 / +18.75 / +19.26 | 56.6% / 17.5% / 17.5% | +1.5% / +62.2% / +36.3% |
| performance-weighted (live) | same | +0.27 / +17.94 / +19.49 | 52.5% / 17.4% / 17.4% | +1.5% / +57.6% / +36.1% |
| **regime-aware** | same | **−0.23 / +18.00 / +19.32** | **52.3% / 17.0% / 17.0%** | −1.3% / +57.2% / +35.4% |

* **Failed checks:** expectancy below equal weights in `all` (−0.23 vs +0.27 bps) and `2015+` (18.00 vs 18.75);
  below the live arm in `all` and `2020+` (19.32 vs 19.49). Minimum margin −0.75 bps.
* **Passed checks:** drawdown is lower than both references in every window; the multiplier resized 6,259 / 2,905 /
  1,800 trades.
* **Confidence intervals include zero** for the weighted expectancy in every window and every arm (for example
  regime-aware 2015+: [−4.0, +42.2] bps). The differences between arms are far inside them. The verdict is "no
  evidence for", not "evidence against".
* The trade set is identical across arms by construction: weights change size, never which trades the book takes.

### The live performance weighting, as a by-product

The regime-blind weight that is on today behaves the same way: lower drawdown everywhere, expectancy a little lower
in 2015+ and a little higher in 2020+. Its own evidence came from paper trades, not this replay; this is a note,
not a verdict.

## What the per-regime record looks like

Descriptive, over the whole history, book trades only (not causal; not the multiplier's input). Cells with ≥ 30
trades:

| Strategy | Trades | Best cell (trades, bps) | Worst cell (trades, bps) |
|---|---:|---|---|
| bollinger_mean_reversion | 290 | down/mid/strong (49, +94) | down/mid/weak (47, −161) |
| channel_breakout | 917 | down/low/strong (48, +62) | up/low/strong (853, +42) |
| ensemble | 1,504 | down/high/weak (34, +120) | down/mid/unknown (33, −138) |
| ibs_mean_reversion | 682 | down/mid/strong (83, +48) | down/mid/weak (135, −25) |
| macd_momentum | 2,250 | up/low/weak (35, +159) | up/mid/weak (154, −98) |
| moving_average_crossover | 3,464 | down/low/weak (31, +297) | up/mid/unknown (54, −123) |
| rsi2_pullback | 630 | up/mid/strong (113, +44) | up/low/strong (499, +1) |
| rsi_mean_reversion | 537 | up/mid/strong (44, +107) | down/mid/weak (76, −62) |
| trend_pullback | 1,273 | down/mid/strong (89, +106) | down/mid/weak (51, −112) |

Per axis, all strategies pooled: breadth strong +9 bps (9,122 trades) vs weak −29 (2,032); VIX low +10 (7,031),
mid −23 (3,491), high +10 (1,025); SPY trend up +2 (8,605) vs down −4 (2,942). The cells differ a lot, which is why
the question was worth asking. Why the multiplier still does not help:

* **The cell record is thin at decision time.** The live rule reads the strategy's last 20 book trades inside 60
  days and needs 5 of them in today's cell. The multiplier changed the size of 54% of trades (`all`), 55% (2015+)
  and 55% (2020+); the rest fell back to the overall weight.
* **Cells change faster than 20 trades accrue.** A VIX band or breadth flip moves the book into a cell whose last
  evidence is weeks old, so the multiplier reacts to the previous stretch of that cell, not to the one the book is
  entering. The `all` window, where it costs 0.5 bps per trade, holds the regime changes of 2008 and 2020; this
  report did not isolate which stretches drive the gap.
* **The product shrinks twice on the same trades.** The cell trades are a subset of the overall 20, so a losing cell
  is counted in the base weight and again in the multiplier. This is by design (shrink before growing) and it does
  lower drawdown, but at the cost of expectancy per unit of risk.

## What was built (off by default)

* `app/services/regime_allocation.py`: the regime cell (`trend/volatility/breadth`: SPY above its 50-day average
  from `market_gate.market_state`; VIX < 20 / 20–30 / > 30; ≥ 50% of S&P 500 members above their 200-day average),
  the current cell from the stored SPY history and the latest fresh gauges, each closed paper round trip's cell
  (trend from SPY closes strictly before the entry date; VIX and breadth from the attribution's diagnosis), the
  per-strategy record per cell, and the bounded multiplier. A cell with fewer than `paper_allocation_min_trades`
  trades falls back to the overall record (multiplier 1.0). An axis whose input is missing is `unknown` and only
  matches trades with the same gap, so an outage falls back rather than inventing a regime.
* `strategy_allocation.compute_weights`: when `paper_allocation_regime_enabled` is on, the weight becomes
  `clamp(overall × cell multiplier)` inside the same floor (0.5) and ceiling (1.25), then the concentration cap. The
  cell, the cell multiplier and the base multiplier are stored in the weight's evidence.
* Every sized BUY records `features.allocation` on its signal (multiplier, status, regime cell and cell multiplier),
  so attribution can see how the entry was sized.
* The learning scoreboard has a `regime` section: the cell in force, each active strategy's record per cell, the
  multiplier the current cell would give, and the stored weight for the session.

## Data and method

* **Data:** the 2026-11-06 cache from `market-gate-20261106.md` (45 paper symbols, 2000-01-01 → 2026-11-05), the
  VIX close history from `vix-gate-20261106.md`, and the earnings cache from `gate-cost-20261106.md`.
* **Candidates:** every executable trade of the 9 replayable live strategies from `gate_replay.simulate` under the
  gates live today (SPY market gates, Channel Breakout's VIX < 20 gate, the 10-session earnings blackout): 60,151.
* **Book:** the live caps (1 per symbol, 5 per strategy, 3 per sector, 20 total), slots filled in the live cycle
  order, and the 10-session stop-out cooldown adopted in `slot-allocation-20261106.md`
  (`run_slot_allocation_backtest.allocate`): 11,547 trades.
* **Cells:** SPY's close above its 50-day average on the decision bar; the VIX close on or before the decision date;
  **breadth as the share of the 45 paper symbols above their own 200-day average** on the decision bar. That is a
  proxy: the live gauge reads the S&P 500 members, whose 26-year history is not cached. The first 200 bars have no
  breadth (`unknown`).
* **Weights:** per entry day, from the strategy's book trades *closed strictly before that day* (last 20 inside 60
  days), the same `performance_multiplier`, floor, ceiling and concentration cap as the paper path. Positions are
  weight / 20 of equity, marked to market daily, so the weighted arms may be up to 125% invested like the live book.
* **Weighted expectancy** is Σ(weight × net return) / Σ weight: the return per unit of notional the arm deployed;
  the bootstrap CI (clustered by month) is on each trade's return scaled by its weight over the mean weight.
* Runtime under a minute (`scripts/run_regime_allocation_backtest.py --data-dir <dir> --vix-csv <csv>
  --output <json>`).
* **Caveats:** survivorship in today's megacap list flatters every arm about equally; the breadth proxy is not the
  live gauge; the paper path's cell comes from the latest *fresh* gauges, so a stale gauge day is `unknown`.

## Re-asking the question

The per-regime table in the scoreboard accrues from forward paper trades. When a strategy shows 30 or more closed
trades in a cell with an expectancy outside the overall CI, that is the trigger to re-run this script on forward
paper trades under the same rule; until then the setting stays off.
