# Feed and benchmark hygiene, 2026-11-07

Ranked experiment 6 of `Frozen stock research priorities.md` (topic 4).

**Question:** the bot decides on consolidated daily bars, takes its decision price from the free IEX feed
(about 3% of US volume), is filled by Alpaca paper, and is judged by backtests that fill at the consolidated
open. Where do these disagree in a systematic way, and how large is each disagreement?

**Short answer:**

- The daily signals already use the consolidated tape.
- The decision price, the paper fills, the app-side stops and one gate-cost input used IEX. They are now
  benchmarked against delayed SIP or switched to it.
- Over the last 20 sessions, IEX missed 2.1% (33 of 1,564) of the stop and target touches that the
  consolidated tape printed. It never printed a touch that SIP did not.
- The backtester already filled gapped stops at `min(stop, open)`. Filling them at the stop would have
  overstated replay expectancy by 7.4 bps per trade, across 6.1% of 79k replayed trades.
- No local database holds paper fills, so the IEX-vs-SIP slippage numbers on real fills come from the first
  run of `feed_hygiene_job`.

Everything here is paper only. Nothing places, changes or cancels an order. No strategy is written, and no
migration is added (audit rows only).

## 1. What each input reads today (verified)

| Input | Source | Tape |
|---|---|---|
| Daily decision bars for every rule, gate and replay | `MarketPrice` via `trusted_history` (yfinance, Yahoo chart fallback) | consolidated |
| Daily bars when Yahoo is down | was `alpaca_iex_daily`; now `alpaca_sip_daily` first, IEX only if SIP is refused | was IEX, now SIP |
| Decision price (entry limit price, sizing, `_validate_reference_price`) | `trusted_intraday_observation`: latest `alpaca_iex` 1m close | IEX |
| App-side protective stop (`_protective_stop`) | same IEX observation | IEX |
| Broker bracket stops and targets | Alpaca, elected on the consolidated tape | SIP |
| Gate-cost replay "opening print" (`gate_cost.intraday_open`) | first 1m bar of any provider, IEX included | now SIP only, else the daily open |
| Backtests and experiment replays | daily bars, entry at the next open | consolidated |

Daily-bar strategies decide inside the session (the entry window runs 09:35-10:15 ET,
`paper_daily_bar_entry_window_*`), not after the close. On the free plan the consolidated tape is only
available 15 minutes late. So the decision price stays on IEX, and the cost of that choice is measured
rather than switched. Every stock paper signal now records its inputs in `features.feed`:

```json
{"daily_bars": "consolidated", "daily_source": "database:yfinance",
 "reference_price": "iex", "reference_provider": "alpaca_iex", "decision_phase": "session"}
```

A history served from `alpaca_iex_daily` shows `daily_bars: "iex"`. That source can win `get_price_history`'s
"freshest source" pick when the paper-short pilot has imported IEX daily rows for a symbol and yfinance lags
by a day, so the tag makes that case visible.

## 2. What was built

- **`app/integrations/alpaca_market_data.py`** is a read-only `/v2/stocks/bars` client for any symbol and
  either feed.
  - A SIP query is refused (`DelayedSipTooRecent`) before any request unless it ends at least 16 minutes
    before now: the free plan's 15 minutes plus one minute of margin.
  - Requests are paced 0.35 s apart (about 170 per minute against the plan's 200), a 429 is retried with
    exponential back-off, and a run is capped at 120 requests.
  - Credentials go only in headers.
- **`app/services/feed_hygiene.py`** holds the feed-hygiene logic.
  - *SIP reference per fill.* An entry at the open is referenced to the official SIP open, which is the open
    of the session's first regular-session SIP bar (flagged when that bar is not the 09:30 bar). "At the
    open" means a BUY entry filled in the first 30 minutes, the bucket the calibration applies to the
    replay's next-open entry, or any `opg` order. Every other fill is referenced to the VWAP of the SIP
    minute bar that contains it, or that bar's open when it has no VWAP. Both candidates are stored, along
    with `sip_bps`, `minute_bps` and `open_bps`. Fills are referenced once, in `fill_sip_reference` audit
    rows.
  - *Fill tags.* Each fill records its time in force, whether it was a paper fill (from the account's broker)
    and whether it was an auction order. Paper `opg` and `cls` fills get `excluded: "paper_auction"`.
  - *Touch log.* For each session, every stop and target level live during it is collected: broker stop
    legs and stop orders that were open or that filled or were cancelled that day, take-profit legs, and the
    app-side protective stop of each held position. The session's regular-hours IEX low/high is compared
    with the SIP low/high, and one `feed_divergence` audit row is written per session. The row lists
    `iex_only`, `sip_only`, `both` and `neither` touches, the first-touch minute on each feed, and how far
    the SIP extreme was from the level.
- **`feed_hygiene_job`** runs on weekdays at 16:40 ET on the `market_data` queue, after the SIP delay. It
  records the references, then the touch log.
- **Trade attribution.** The `slippage` block keeps `entry_bps` and `exit_bps` against the decision price
  (`reference_feed: "iex"`). It adds `sip_reference.entry` and `sip_reference.exit` (quantity-weighted over
  each side's fills), `sip_bps.entry` and `sip_bps.exit`, `entry_time_in_force`, `paper` and `auction`.
- **Cost calibration (PR #24).**
  - Entries, market orders and fills without an order are scored against the SIP reference when one
    exists. Protective-stop and take-profit legs keep their level, because that is what the replay prices
    them against.
  - Each row keeps `decision_slippage_bps` (the IEX number) and `sip_slippage_bps`.
  - Paper auction fills are kept and tagged, but they are not usable and are not averaged.
  - The report gains `feed` (IEX vs SIP means on fills that have both benchmarks, by order kind, plus
    exclusions by time in force), `fills.excluded_paper_auction`, `fills.sip_referenced`, and
    `by_reference_feed` / `by_time_in_force` tables.
  - The weekly report's `cost_calibration` section gains a `feed_hygiene` block: the week's touch-log
    summary, SIP-priced fills, and excluded auction fills.
- **`market_data.fetch_alpaca_sip_daily_prices`** fetches delayed consolidated daily bars. It is tried
  before the IEX daily fallback, and `alpaca_sip_daily` is added to every trusted-source set.
- **`gate_cost.intraday_open`** reads only consolidated 1m bars (`sip`, `sip_delayed`). An IEX-only first
  trade no longer replaces the consolidated daily open in gate-cost replays.
- **`triple_barrier.gapped_stop_fill`** makes the existing gap rule explicit and tested (see §4).
- **`scripts/feed_hygiene_report.py`** produced the numbers below, and `tests/test_feed_hygiene.py` adds 22
  tests.

## 3. IEX vs SIP on the last 20 sessions

The data covers 45 paper-universe symbols over the 20 sessions from 2026-10-12 to 2026-11-06: IEX and
delayed SIP minute bars, 80 requests. No paper fills exist locally, so the stop and target levels are
hypothetical. They sit 1%, 2% and 3% from the prior consolidated close, on both sides, giving 5,400
symbol-session-level checks.

| Measure | n | mean | median | p90 | max |
|---|---|---|---|---|---|
| IEX vs official SIP open, absolute (bps) | 883 | 8.66 | 4.52 | 21.9 | 117.5 |
| IEX minus official SIP open, signed (bps) | 883 | -1.37 | 0.00 | 12.06 | 85.9 |
| IEX vs SIP 09:39 minute close (decision-price proxy), absolute (bps) | 883 | 2.77 | 1.42 | 7.06 | 55.1 |
| IEX minus SIP 09:39 minute close, signed (bps) | 883 | +0.14 | 0.00 | 4.66 | 55.1 |
| SIP high above IEX high (bps) | 900 | 3.17 | 1.26 | 6.86 | 83.2 |
| IEX low above SIP low (bps) | 900 | 4.60 | 1.44 | 11.61 | 240.5 |
| IEX minute bars per SIP minute bar | 900 | 0.92 | 0.97 | 1.00 | 1.00 |

On 17 symbol-sessions IEX had no 09:30 bar at all.

| Level | SIP touched | IEX missed | Share of SIP touches missed | IEX-only touches |
|---|---|---|---|---|
| stop 1% | 404 | 8 | 2.0% | 0 |
| stop 2% | 225 | 6 | 2.7% | 0 |
| stop 3% | 121 | 3 | 2.5% | 0 |
| target 1% | 460 | 9 | 2.0% | 0 |
| target 2% | 231 | 5 | 2.2% | 0 |
| target 3% | 123 | 2 | 1.6% | 0 |

At least one level diverged on 17 of the 20 sessions. Two examples: on 2026-10-15 ORCL's consolidated low
was 127.60 against an IEX low of 128.02, a 2% stop at 127.94. On 2026-10-13 PEP's SIP high was 128.15
against an IEX high of 127.94, a 1% target at 127.95.

**Reading.** IEX prints are a subset of the consolidated tape, so IEX never shows a touch that SIP lacks.
Every disagreement is SIP touching a level that IEX missed. For the bot this means two things:

- A broker bracket stop or target (elected on SIP) can fill on a day when the IEX-based record shows no
  touch.
- The app-side protective stop (IEX) fires late or not at all on roughly 2-3% of the days the consolidated
  low crosses it.

The backtests use consolidated bars, so the broker legs match them and the app-side stop does not. This
supports the research recommendation to treat the broker stop as the primary protection. The touch log now
measures the same rate on the real levels every session.

**Opening price.** The IEX first trade misses the consolidated opening print by a median of 4.5 bps, and by
22 bps at the 90th percentile. Before this change, `gate_cost` replays took that IEX open as "the real
opening print" whenever a 1m bar existed. It now uses only SIP bars, or else the consolidated daily open.

**Decision price.** At 09:39, which approximates the decision time, the IEX minute close sits within 1.4 bps
of the SIP minute close at the median (2.8 bps mean absolute), and the signed mean is +0.14 bps. So the IEX
decision price adds noise to measured slippage but no meaningful bias. That is consistent with keeping it as
the live price and benchmarking fills against SIP after the close. The open is noisier: the signed mean is
-1.4 bps, so IEX opens slightly below the consolidated print on average.

## 4. Gapped stops in the backtester

`triple_barrier.label_events` checked each bar's open before its range. An open at or below the stop already
exited at the open (`stop_loss_gap`), and `backtester.run_backtest` did the same. So the rule was already
`min(stop, open)` and nothing changes in today's replays. It is now an explicit helper,
`gapped_stop_fill(stop, open) = min(stop, open)`, with a test: a 6% gap-down against a 3% stop exits at the
open (-6%), and an intrabar touch without a gap still exits at the stop (-3%).

To size what the rule protects against, every paper strategy was replayed as the experiment engine replays it
(`gate_replay.simulate`, published cost model, 45 symbols, 2000-01-01 to 2026-11-06). For each gapped stop,
the return if it had filled at the stop instead was computed. Costs are multiplicative, so this
counterfactual is exact.

| Strategy | trades | stop exits | gapped | share of stops | mean gap below stop (bps) | overstated per gapped trade (bps) | per trade (bps) | expectancy (bps) | if filled at the stop |
|---|---|---|---|---|---|---|---|---|---|
| moving_average_crossover | 21,180 | 12,487 | 1,380 | 11.1% | 128 | 124 | 8.1 | -8.5 | -0.4 |
| macd_momentum | 17,237 | 10,389 | 1,206 | 11.6% | 129 | 125 | 8.7 | -12.1 | -3.4 |
| ensemble | 11,787 | 7,122 | 754 | 10.6% | 124 | 120 | 7.7 | -4.1 | +3.6 |
| trend_pullback | 10,072 | 5,575 | 650 | 11.7% | 110 | 106 | 6.9 | +5.8 | +12.7 |
| channel_breakout | 6,133 | 3,302 | 451 | 13.7% | 121 | 117 | 8.6 | +2.0 | +10.6 |
| rsi2_pullback | 5,942 | 268 | 55 | 20.5% | 265 | 238 | 2.2 | +16.5 | +18.7 |
| rsi_mean_reversion | 2,358 | 1,326 | 167 | 12.6% | 99 | 96 | 6.8 | +16.4 | +23.2 |
| ibs_mean_reversion | 2,192 | 44 | 12 | 27.3% | 151 | 135 | 0.7 | +13.0 | +13.7 |
| bollinger_mean_reversion | 2,086 | 1,191 | 155 | 13.0% | 111 | 107 | 8.0 | +4.2 | +12.1 |
| **all** | **78,987** | **41,704** | **4,830** | **11.6%** | **125** (median 60, p90 299) | **120** | **7.4** | **-2.4** | **+5.0** |

**Reading.**

- About one stop exit in nine is a gap through the stop, at a median of 60 bps below it.
- A replay that filled those trades at the stop would add 7.4 bps to every trade's expectancy. That would
  turn the pooled book from -2.4 to +5.0 bps, and ensemble and moving_average_crossover from negative to
  roughly flat or positive.
- The 10% stops (RSI(2), IBS) gap less often in absolute terms but by more when they do (238 bps per gapped
  trade).
- The current replays already charge this cost, so their verdicts stand. Any new backtest path should reuse
  `gapped_stop_fill` and not price stops at the level.

## 5. Paper OPG fills

The bot's entries are bracket limits (`gtc` or `day`), and nothing in the order path sends `opg` or `cls`
today. So the exclusion currently removes 0 fills. It exists so that any adopted or experimental auction
order cannot teach the calibrator Alpaca paper's quote-based imitation of the auction. That imitation fills
at the quote, with random partials, and cancels after 30 s; on one sampled day about 60% of paper OPG orders
were cancelled. `fills.excluded_paper_auction` and `feed.excluded_by_time_in_force` report the count every
Saturday. A live (non-paper) auction fill is tagged but kept.

## 6. What the next runs will show

The first `feed_hygiene_job` run after deploy references the last 10 sessions of fills. The Saturday
calibration then shows, per order kind, the mean slippage against the IEX decision price next to the mean
against SIP (`report.feed.by_order_kind`).

For entries, SIP-referenced slippage is measured against the official open. So it contains the drift from
the open to the 09:35-10:15 fill as well as the spread. That drift is real backtest/live mismatch, because
the replay fills at the open. `minute_bps` on every reference isolates the execution-only part.

Expect every attribution record to gain one revision on the first run, because its digest changes with the
new keys.

## Not done / follow-ups

- The decision price stays IEX. A free real-time consolidated price does not exist, and changing the limit
  price would be order-placement logic. The tag and the SIP benchmark measure its cost instead.
- `get_price_history` still picks the freshest stored source. On a tie in date it can prefer
  `alpaca_iex_daily` over yfinance when the IEX rows outnumber yfinance rows. Preferring consolidated sources
  there would close the last IEX path into daily signals. That change touches every daily reader, so it is
  left for a separate change.
- Ranked experiments 11 (limit-on-open gap filter) and 14 (09:45/10:00 entry on SIP minute bars) can reuse
  `AlpacaBarsClient` and `official_open` directly.
