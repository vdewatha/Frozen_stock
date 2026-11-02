# Strategy discovery: 2026-11-02

Branch `feat/strategy-discovery`. Lets the bot look for new strategy families instead of only selecting and tuning
the hand-written ones. None of those has a net edge that survives costs and selection
(`strategy-scorecard-20261028.md`, `meta-label-learning-20261102.md`).

## How it works

1. **Rule language** (`strategies/rules.py`). A long-only daily-bar rule is plain data: entry conditions that must
   all hold, plus exit conditions where any one is enough. It uses causal features: returns, Wilder RSI, distance to
   SMA, N-day highs and lows, up/down streaks, IBS, the opening gap, Bollinger %B, ATR%, a realised-volatility ratio,
   and the weekday calendar (trading day of month, days left in month, day of week). The stop, target and time stop
   are ordinary strategy parameters. Each discovered rule gets its own slug, `rule_<id>`, which
   `registry.get_strategy` resolves to `RuleStrategy`, so attribution, sizing and meta-label modes stay separate per
   rule. Tests check that, on every prefix, the vectorised rule matches the bar-by-bar strategy for entries and
   exits.
2. **Candidate library** (`strategy_discovery.candidate_library`). It has 88 hypotheses from the published anomaly
   literature, each tested on 3 universes, for **264 trials**. The families are short-term pullbacks in an
   uptrend, N-day lows, down streaks, turn of the month, Bollinger lows, weekly reversals, breakouts, gap-down
   recoveries and calm-market dips.
3. **Evaluation**. Each candidate is replayed with the same execution model as the backtester:
   * decision on the close, fill at the next open;
   * volatility-scaled costs on both sides;
   * stop first inside a bar, gaps filled at the open;
   * indicator exit at the next open;
   * one position per symbol.

   Daily portfolio returns are marked to market. Data is yfinance adjusted daily bars, 2000 → 2026-10-30, for the
   45 paper symbols. The run takes about 25 s on 10 cores.
4. **Pass/fail bar, fixed in advance** (`GATE`). It is computed on the discovery window, which is everything before
   the last 2 years:
   * ≥ 100 trades;
   * the month-clustered bootstrap lower bound on **net** expectancy is > 0;
   * the lower bound on **excess over the symbol's own drift** is > 0, because holding a stock that went up is not
     an edge;
   * ≥ 30 trades with positive expectancy since 2015;
   * a deflated Sharpe ratio ≥ 0.95 that counts all 264 trials;
   * the 2-year holdout has ≥ 20 trades and positive expectancy.

   * **proven** means every check passes.
   * **explore** means every check passes except the deflated Sharpe: worth a forward test, not a claim of edge.
5. **Admission**. Each weekly run (Sunday 10:00 New York time, learning queue) refreshes the evidence on discovered
   rows:
   * New `explore` or `proven` candidates become `rule_<id>` Strategy rows with status `paper_trading_candidate`.
     That status means small, tagged exploration orders only, and promotion still needs the normal forward-trial
     gates.
   * At most 3 are admitted per run and at most 10 discovered strategies can be live.
   * A candidate that stops qualifying is retired. A strategy the forward loop has already promoted is left to its
     forward evidence.
   * `strategy_discovery_require_proven=true` admits proven candidates only.

## Results

**Proven: 0 of 264. Explore: 2.**

| Tier | Rule | Universe | Trades | Win % | Exp. bps [95% CI] | Excess over drift [CI] | 2015+ | Holdout (trades, bps) | DSR |
|---|---|---|---:|---:|---|---|---:|---|---:|
| explore | close > SMA200, vol20/vol100 < 1, RSI(2) < 5; exit close > SMA5 | index ETFs | 229 | 69.4 | +43.7 [18.8, 68.6] | [4.5, 55.7] | +34.2 | 37, +41.9 | 0.12 |
| explore | close > SMA200, 5-day return < −5%; exit close > SMA5 | sector ETFs | 313 | 70.0 | +64.9 [18.9, 107.2] | [2.4, 91.7] | +83.0 | 41, +79.0 | 0.05 |

What the other 262 failed on:

| Check failed | Candidates |
|---|---:|
| Deflated Sharpe | all 264 |
| Excess over drift | 258 |
| Net expectancy CI | 225 |
| Post-2015 | 94 |
| Holdout | 81 |

* **Single-stock breakouts look great, and that is survivorship.** Net expectancy was +99 to +172 bps, but the
  universe is today's megacaps (NVDA, AMD, AVGO…). Against each stock's own drift the excess is −43 bps
  [−88, +2], so the excess-over-drift check correctly rejects them.
* **The closest thing to the 8-out-of-10 goal.** "5-day drop > 8% in an uptrend" wins 87–93% of the time at
  +149 to +175 bps per trade. It fires only 11–47 times in 22 years and has no holdout trades, so it is rejected:
  too rare to trust.
* **Why the deflated Sharpe fails everywhere.** The explore candidates are in the market only part of the time.
  Their annualised Sharpe of 0.4–0.5 doesn't beat the 0.72 that luck alone would produce across 264 trials. Only
  forward evidence can settle that, which is what the explore tier is for.

## Bugs caught while building

* The first portfolio series spread each trade's return evenly over its holding days. That hid the daily
  volatility, inflated Sharpe ratios (3.2 for single-stock breakouts), and with them the DSR bar for everyone. It now
  marks to market daily, and a test checks that the compounded daily returns equal the trade's return.
* `validate_rule` could not reject a lookback longer than the paper frame, because `rule_lookback` capped the value
  before the check.

## Caveats

* The holdout is re-read every week, so over time it becomes a sanity check, not a clean test set.
* The library and the gate were written before these results. The explore tier (everything except the deflated
  Sharpe) was added after seeing that nothing was proven. It only affects paper exploration, never promotion, and
  can be switched off.
* Expect about 25 explore trades a year from the two candidates, so the forward evidence will take months.
