# Slot Allocation: Ranking, Caps and the Stop-Out Cooldown, 2026-11-06

**Question:** `market-gate-20261106.md` found that the capped paper book picks worse trades than it turns away.
Would ranking competing signals, changing the caps, or not re-buying a symbol right after it stops out fix that?

**Decision rule (fixed before the run, in `scripts/run_slot_allocation_backtest.py`):** a policy is eligible only
if, in every window (all, 2015+, 2020+):
* per-trade net expectancy is higher than the live baseline's;
* book max drawdown is ≤ 1.10× the baseline's; and
* it keeps ≥ 50% of the baseline's trades.

Among eligible policies, the one with the largest minimum expectancy gain is adopted.

## Answer: a 10-session stop-out cooldown

**Adopted: `stop_cooldown_10`.** After a stop-out on a symbol, no strategy re-enters that symbol until the 10th
NYSE session after the stop.

| Policy | Trades (all / 2015+ / 2020+) | Exp. bps, all / 2015+ / 2020+ | Max DD, all / 2015+ / 2020+ | Days with ≥3 stops (all) |
|---|---|---|---|---:|
| **baseline (live)** | 16,978 / 6,874 / 4,465 | −9.9 / +10.0 / +8.9 | 79.9% / 25.8% / 25.8% | 1,202 |
| rank by confidence | 19,069 / 7,936 / 5,122 | −12.9 / +3.7 / +6.9 | 82.9% / 27.4% / 27.4% | 1,394 |
| rank by track record | 17,598 / 7,240 / 4,699 | −10.5 / +4.9 / +6.2 | 78.6% / 26.7% / 26.7% | 1,172 |
| stop cooldown 5 | 13,575 / 5,947 / 3,807 | +1.0 / +18.2 / +18.6 | 58.0% / 20.3% / 20.3% | 893 |
| **stop cooldown 10** | **12,179 / 5,536 / 3,489** | **+3.7 / +19.5 / +17.5** | **51.9% / 18.4% / 15.2%** | **732** |
| max 2 new per day | 11,394 / 4,935 / 2,986 | −1.1 / +16.6 / +13.8 | 55.2% / 18.5% / 18.5% | 651 |
| max 4 new per day | 15,194 / 6,271 / 4,018 | −5.5 / +8.8 / +8.4 | 67.7% / 24.1% / 24.1% | 1,062 |
| looser caps (40 / 10 / 6) | 26,128 / 11,309 / 7,348 | −7.3 / +9.4 / +6.0 | 64.1% / 21.4% / 21.4% | 1,947 |
| one per symbol, no other cap | 30,799 / 13,692 / 8,891 | −8.6 / +8.0 / +4.1 | 67.6% / 21.6% / 21.6% | 2,309 |

* **Eligible:**
  * stop cooldown 10 (minimum gain +8.6 bps);
  * stop cooldown 5 (+8.2);
  * max 2 new per day (+4.9).
* **Not eligible:** both rankings lower expectancy, and neither looser caps nor removing the caps helps. So the
  caps were not the cause.
* **Book total return with the cooldown:** all history −58.7% → +24.1%, 2015+ +37.3% → +70.1%, 2020+ +19.9% →
  +35.1%. Each position is 5% of equity, as in the backtest. Win rate rises 2–3 points, to 43–44%.
* **Confidence intervals still include zero** for the per-trade expectancy, in every window. The gains are
  consistent across all three windows, but they are not proof.

## Why: re-entries after a stop

Under the live allocation, 9,312 of the book's 16,978 trades (55%) re-entered a symbol within 10 sessions of a
stop-out on it. Those re-entries averaged **−46.5 bps**; the other 7,666 trades averaged **+34.5 bps**. A
strategy with a standing BUY (for example a trend or MACD signal on a falling stock) buys again right after its
stop, and is stopped again. The live 30-minute exit cooldown only delays the re-buy to the next cycle or day.

With the 10-session cooldown, the trades the book accepts beat the ones it turns away:

| | all | 2015+ | 2020+ |
|---|---|---|---|
| Baseline: accepted vs turned away (bps) | −9.9 vs +3.4 | +10.0 vs +18.7 | +8.9 vs +15.2 |
| Cooldown 10: accepted vs turned away (bps) | +3.6 vs −0.8 | +19.5 vs +16.2 | +17.5 vs +13.0 |

## Correction to `market-gate-20261106.md`

That report said the capped book's accepted trades averaged −46.0 bps against +16.6 bps for the turned-away ones.
That simulation filled same-day slots in a fixed strategy order that put MA Crossover, RSI(14) and MACD first.
The live cycle fills them in symbol-list order. Under the live order, the gap is real but smaller (table above),
and the cause is the re-entries after a stop, not the order in which slots are filled. The market-gate decisions
in that report used per-strategy results, not the book, so they are unchanged.

## Live change

New entry protection `stop_cooldown` (`entry_protections.stop_cooldown_block`):
* After a stop-out on a symbol (a broker stop leg, or the autotrader's protective stop), every strategy's new
  entry on that symbol is withheld until the start of the 10th NYSE session after the stop's session.
* It's set by `stop_cooldown_sessions` (default 10), read from the active risk rule like every other protection;
  0 turns it off.
* Exits are never affected.

## Data and method

* **Data:** the 2026-11-06 cache from `market-gate-20261106.md`: 45 paper symbols, 2000-01-01 → 2026-11-05.
* **Candidates:** every executable trade of the 9 replayable live strategies, with the PR #13 market gates, from
  `run_market_gate_backtest.simulate`. That gives 67,220 trades, which average +0.0 / +16.8 / +13.8 bps taken
  without any cap.
* **Book:**
  * slots are filled day by day in the live cycle order (symbol list, then strategy);
  * the live caps apply: 1 per symbol, 5 per strategy, 3 per sector, 20 in total;
  * each position is 1 / total cap of equity, so every policy is at most 100% invested;
  * returns are marked to market daily.
* **Track record** (for ranking) is the mean net return of the strategy's candidate trades that closed strictly
  before the day, so it is causal.
* Runtime is about 3.5 minutes (`scripts/run_slot_allocation_backtest.py`).
* **Caveat:** survivorship in today's megacap list flatters every row about equally.
