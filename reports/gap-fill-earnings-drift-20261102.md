# Opening-gap fill and post-earnings drift: 2026-11-02

Branch `feat/gap-fill-earnings-drift`. Adds two trading styles to the weekly strategy discovery library
(`strategy-discovery-20261102.md`): a **day trade**, the opening-gap fill, and a **fundamental + sentiment** swing
trade, post-earnings announcement drift (PEAD).

## What changed

* **Earnings history** (`services/earnings_history.py`). yfinance has about 100 quarters of EPS surprises per stock,
  back to 2002, each with a New York report time. Each report is placed on the first session that could trade it:
  * before 09:30 → that day;
  * otherwise → the next session.

  Reports are dropped when:
  * the time is midnight (unknown), because guessing the session would add a day of look-ahead or lag;
  * the surprise is missing or above 500%;
  * two reports share a day or a reaction bar.

  Discovery reads a weekly-refreshed CSV cache next to the price history. The paper path fetches through a 12-hour
  per-process cache with a timeout, and if that fails the rule holds.
* **Rule language** (`strategies/rules.py`):
  * New feature `earn_surprise`: the surprise in percent on the reaction bar, NaN on every other bar.
  * New `"timing": "open"`: the rule buys at the open of bar t when the `gap` condition holds on bar t and every
    other condition held at the previous close. Open rules must have a gap condition and no exit conditions.
  * `RuleStrategy` never fires an open rule, because the daily paper path cannot place an order at the open.
* **Same-session labeler** (`triple_barrier.label_open_session`):
  * Enter at the open with the usual volatility-scaled cost.
  * The target is the prior close (gap filled), with a stop below entry.
  * Otherwise exit at the close. When one bar touches both levels, the stop is assumed first.
* **Library**: 78 new trials, for **342** in total. More trials make the deflated Sharpe stricter for every
  candidate.
  * `earnings_drift` (24, single stocks only):
    * surprise > 0 / 5 / 10%;
    * with or without a reaction-day return above 2%;
    * with or without the 200-day trend filter;
    * held 20 or 40 sessions with a 10% stop.
  * `opening_gap_fill` (18 per universe, 54 in all):
    * a gap below −0.3 / −0.5 / −1% but not past −3%;
    * no filter, uptrend, or calm volatility;
    * a 1% or 3% stop.
* **Admission**: open-timed candidates are graded and reported (`paper_executable: false`) but never admitted.

The gate (`GATE`) is unchanged.

## Results

Data: yfinance adjusted daily bars and earnings history for the 45 paper symbols, 2000 → 2026-10-30. The holdout
starts 2024-10-30.

**Proven: 0 of 342. Explore: 2**, the same two as before (calm-market RSI(2) dip on index ETFs, and the 5-day drop in
an uptrend on sector ETFs). Neither new family produced a survivor.

### Opening-gap fill: no edge, even before costs

| Universe | Gap −0.3% to −3%, 3% stop | Trades | Win % (net) | Price move, no costs | Costs | Net bps |
|---|---|---:|---:|---:|---:|---:|
| Index ETFs | no filter | 6,151 | 56 | −0.9 | 26.7 | −27.6 |
| Single stocks | no filter | 43,681 | 58 | −4.7 | 35.3 | −40.0 |

* All 54 variants lose, from −19 to −49 bps per trade net, and every one fails the post-2015 and holdout checks
  too.
* It *looks* like the 8-out-of-10 goal: before costs, 62–66% of trades win, and about half the gaps do fill.
  But gaps that don't fill keep falling, and those losses cancel the small wins, so the average is about 0 bps
  before costs.
* The cost model charges about 27 bps round trip on ETFs, which is pessimistic for SPY. Even at zero cost there is
  nothing to earn.
* Building at-the-open execution for this would not pay. Drop the idea unless 1-minute data shows a narrower setup.

### Post-earnings drift: profitable, but it doesn't beat simply holding the same stocks

| Rule (single stocks) | Hold | Trades | Win % | Net bps [95% CI] | Excess over drift | Holdout (trades, bps) |
|---|---:|---:|---:|---|---:|---|
| surprise > 0 | 40 | 1,981 | 59 | +215.7 [138, 289] | −69 | 191, +118 |
| surprise > 10 | 40 | 662 | 55 | +293.6 [162, 429] | −28 | 41, +217 |
| trend, surprise > 5, reaction > 2% | 20 | 395 | 62 | +179.5 [84, 274] | CI [−90, +94] | 31, +313 |

* Every variant has a clearly positive net expectancy that held up after 2015 and in the holdout. All 24 still fail
  **excess over drift**: buying these stocks after a beat earns less than holding them on random days.
* The universe is today's megacaps (NVDA, AVGO, LLY…), whose 25-year drift is huge, so the excess-over-drift check
  is the right one to apply. This is the same survivorship trap as the single-stock breakouts in the first
  discovery run.
* Win rates are 53–62%, far from 80%.
* PEAD is not disproven. It just can't be shown on a universe chosen with hindsight. A fair test needs
  point-in-time index membership, meaning the stocks that were in the S&P 500 *at each date*, including ones that
  later fell out.

## What runs in production after merge

* The weekly `strategy_discovery_job` grades all 342 candidates and fetches earnings history for single stocks
  (about 30 extra yfinance calls a week, cached for 7 days).
* Nothing new will be admitted to paper on today's data:
  * gap-fill candidates can't be admitted (no at-the-open execution);
  * PEAD candidates are admitted only if a future run passes the gate.
* If a PEAD rule is ever admitted, the paper path fetches the surprise history at signal time and holds when it is
  unavailable.
