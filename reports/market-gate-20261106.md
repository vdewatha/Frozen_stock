# SPY Market Gate and Position Caps, 2026-11-06

**Question:** the loss diagnosis says 14 of the 26 paper losses so far (54%) are `missed_market_move`. The whole
market fell and took the position with it, and 23 of 34 trades had stops that hit together. Would "no new longs
while SPY is falling" or a tighter cap on open longs have helped, judged on 26 years of history rather than one
bad week?

**Decision rules (fixed before the run, same as `regime-filter-20261029.md` / `regime-gate-20261106.md`):**

* **Market gate.** Gate N ∈ {10, 20, 50} is eligible for a strategy only if:
  * net expectancy per trade is not worse than ungated in any window (all, 2015+, 2020+); and
  * equal-weight portfolio max drawdown is ≤ 1.10× ungated in every window.

  Among the eligible values, the one with the largest minimum expectancy gain across windows is adopted.
* **Position cap.** A total cap K < 20 on the whole book is adopted only if per-trade expectancy is not worse in
  any window AND max drawdown improves in every window.

## Answer

| Strategy | Adopted | Why |
|---|---|---|
| MACD Momentum | **SPY > SMA50** | Expectancy up in every window (−12.0 → −4.0, +15.4 → +18.9, +13.7 → +17.7 bps); drawdown 65.8% → 45.4%, 14.5% → 13.7%, 11.0% → 9.6% |
| Channel Breakout | **SPY > SMA20** | +2.3 → +7.7, +18.5 → +26.1, +21.9 → +36.5 bps; drawdown 16.1% → 12.7%, 6.7% → 7.1%, 4.5% → 3.6%. The only result here whose 2020+ CI clears zero: [+0.3, +76.1] |
| Trend Pullback | **SPY > SMA10** | +5.9 → +12.2, +11.2 → +24.9, +1.2 → +13.4 bps; drawdown roughly halves in every window |
| RSI(2) Pullback | **SPY > SMA50** | +16.7 → +20.5, +13.5 → +18.8, +13.4 → +32.7 bps; drawdown 12.0% → 8.1%, 12.0% → 8.1%, 9.0% → 2.7% |
| RSI(14) Mean Reversion | none | Every gate lowers expectancy in at least one window. It already has the ADX `range` gate. |
| Bollinger Mean Reversion | none | Same as RSI(14): already range-gated, and every SPY gate costs expectancy somewhere |
| IBS Mean Reversion | none | SMA20 helps on full history and 2020+, but 2015+ drops +22.0 → +18.9. It fails by the rule. |
| Ensemble | none | Every gate lowers expectancy |
| MA Crossover | none | No gate improves all three windows |
| Model Predictive Long | not tested | Refits a random forest on every bar, so it can't be replayed over 26 years |

**No tighter position cap is adopted.** Caps of 12, 8 and 5 lower per-trade expectancy in every window, so they
fail the rule.

**Win rate:** the gate moves win rates by 1–2 points (MACD 37 → 39%, RSI(2) 63 → 62–64%). It's a drawdown and
expectancy tool, not a path to 80%. All confidence intervals overlap, so none of these differences is
statistically significant on its own.

## The bigger finding: the capped book picks the wrong trades

Replaying the whole book under today's live caps:
* one position per symbol;
* ≤ 5 per strategy;
* ≤ 3 per sector;
* ≤ 20 in total.

| Set | Trades | Avg net per trade |
|---|---:|---:|
| Every trade the strategies signal (no caps) | 78,971 | −2.3 bps |
| Trades the capped book accepts | 23,845 | **−46.0 bps** |
| Trades the caps turn away | 55,126 | **+16.6 bps** |

Every strategy's accepted trades are worse than its average. For example, Ensemble drops from −4 to −84 bps,
MACD from −12 to −41 and Trend Pullback from +6 to −54. The selection uses no future information:

* Trend-style strategies hold a standing BUY.
* While the market rises, positions run to their 20-session limit or their 6% target, the slots stay full, and
  new (good) signals are turned away.
* Slots free up mainly when positions stop out, which happens in falling markets.
* So the next entries land in falling markets: 47% of accepted entries came with SPY below its 20-day average,
  against 38% of the turned-away ones.

Tighter caps make this worse, which is why K = 12, 8 and 5 fail. With the adopted gates, the book improves in
every window (−46.0 → −39.4, −31.2 → −22.6, −36.8 → −24.4 bps per trade; max drawdown 2020+ 68.5% → 50.7%), but
it stays negative.

**This is a lead, not a change.** It suggests the way slots are allocated loses more than any one strategy does,
and that ranking signals (or not re-entering right after a stop) may matter more than adding filters. Changing
the caps was not part of this PR. It needs its own pre-registered test.

## Live change

* `app/services/market_gate.py`: `spy_above_sma` / `market_state` on completed SPY daily bars. The backtest and
  the paper path call the same function.
* **Entry protection `market_gate`.** A strategy row with `market_gate_sma = N` takes no new entries while SPY's
  last completed close is at or below its N-day average:
  * sells, exits and broker legs are untouched;
  * a SPY load failure fails closed for gated strategies only;
  * the state is in the cycle's `entry_protections` summary.
* **`seed.enable_market_gates`.** A one-time step sets the four adopted values. It skips a row that already has
  the key or an earlier `market_gate_update` event, so an operator's later choice stands. The other strategies
  stay ungated.

## Data and method

* **Data:** yfinance daily OHLCV, split/dividend adjusted, for the 45 paper symbols, 2000-01-01 → 2026-11-05,
  downloaded 2026-11-06.
* **Engine** (`scripts/run_market_gate_backtest.py`, committed): the vectorised strategy replicas in
  `meta_label_primary`, which are parity-tested against the strategy classes.
  * A BUY needs confidence ≥ 0.55, as in the backtester.
  * RSI(14) and Bollinger keep their live ADX `range` gate; RSI(2) uses its widened scope; IBS uses IBS < 0.1 on
    the index ETFs.
  * Exits: the tested 3% stop / 6% target / 20 sessions for the technical strategies; a 10% stop, 10 sessions and
    the indicator exit for RSI(2) and IBS.
  * Fills: `triple_barrier.label_events` (decision on the close, next-open entry, volatility-scaled costs on both
    sides, stop first, gaps fill at the open). One position per symbol per strategy.
* **Metrics:**
  * net expectancy per trade, with a month-cluster bootstrap CI;
  * the equal-weight daily portfolio max drawdown, marked to market;
  * the book simulation uses 5% of equity per position.
* Runtime is about 2.5 minutes on 10 cores.
* **Caveat:** the symbol list is today's megacaps (survivorship), which flatters every row equally. It doesn't
  change the gated-versus-ungated comparison much.
