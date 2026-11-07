# Lesson templates, 2026-11-06

Branch `claude/gracious-brahmagupta-0ca8b0`, the roadmap item "every diagnosed cause gets an automatic
experiment" (`self-evolution-roadmap-20261106.md`).
**Question:** three diagnosed lessons (`missed_sector_move`, `weak_breadth_entry`, `clustered_stops`) had no
experiment template, so the engine reported them weekly and never acted. Can each get a live protection, a
replay and a template under the unchanged pre-registered rule, so that every cause and every contributing
flag the loss diagnosis can name is covered?

**Goal used:** total profit after costs, as everywhere in the engine. Nothing here adopts a value by hand:
every new template ships with the gate *off* and the engine turns it only when the rule passes. Paper only.

## What was built

1. **Sector gate** (`missed_sector_move`). `market_gate.py` is generalised: `above_sma` / `gate_state` work
   on any benchmark, `market_state` is the SPY form, `sector_state(frame, N, etf)` the sector form, and
   `sector_symbol(symbol)` names the SPDR ETF a symbol is gated on (the `trading_universe.sector_group`
   mapping the loss diagnosis attributes against; index ETFs, the sector ETFs themselves, SH and unmapped
   names have none and are never gated). A strategy row with `sector_gate_sma = N` takes no new entry in a
   symbol while its ETF's last completed close is at or below its N-day average. `load_entry_protections`
   loads each needed ETF once per cycle (`sector_gates=`, `symbols=`, `sector_frame=`), fails closed per ETF
   for gated strategies only, and `EntryProtections.sector_gate_block` names the ETF and the symbol in the
   reason (`entry protection sector_gate: XLK 101.00 at or below its 20-day average 104.00; macd_momentum
   takes no new longs in AAPL while its sector falls`). Replay: `gate_replay.sector_gate_on(frame,
   sector_frame, window)`, wired into `simulate` through the setup key `sector_gate_sma` (ETF frames from
   `frames` or `sectors=`). Template `sector_gate`, triggers `missed_sector_move` / `costly_sector_gate`,
   candidates off / 10 / 20 / 50, governance action `sector_gate_update` on the strategy row.
2. **Breadth gate** (`weak_breadth_entry`). `market_gauges.breadth_history(membership, loader)` rebuilds the
   gauge day by day from the point-in-time S&P 500 membership (PR #30) and the cached daily bars: on each
   date, the members *of that date* with 200 completed closes, the share above their average; dates with
   fewer than 100 priced members are unknown. `load_breadth_history` caches it as
   `history_dir()/universe/breadth_above_200d.csv` and rebuilds weekly, keeping the old file when a rebuild
   fails; `breadth_series` is the shape the replay reads. Live: `breadth_gate_min = P` on a row blocks entries
   while the latest gauge (`latest_gauges`, read once per cycle and handed to the protections as `gauges=`)
   is below P; a missing or stale gauge fails *open* as the VIX gate does, and the state's reason says so
   (`breadth gauge missing; the breadth gate fails open and lets the entry through`). Replay:
   `breadth_gate_on(frame, breadth_series, min_pct)`, unknown readings fail open. Template `breadth_gate`,
   triggers `weak_breadth_entry` / `costly_breadth_gate`, candidates off / 40 / 50 / 60, action
   `breadth_gate_update`. The engine loads the history only when a planned experiment needs it and replays
   without the gate (fail open, recorded as `breadth_history: false`) when it cannot.
3. **Correlated-entry cap** (`clustered_stops`). Book-level protection `max_new_entries_per_session` (risk-rule
   key, read through `protection_config`, 0 = off, now an updatable bounded risk field): once that many new
   entries filled in the regular session (distinct buy orders with a fill since the open, plus the BUYs the
   running cycle already submitted via `protections.record_entry`), further BUYs wait for the next session
   (`entry_cap_block`). Replay: the book simulation of `scripts/run_slot_allocation_backtest.py` moved into
   `app/services/book_replay.py` (`candidates` / `allocate` / `metrics` / `book_rows` / `book_policy`); the
   script and the three sibling scripts that import `allocate` keep working on it. Template
   `entry_cluster_cap` (`kind="book"`), triggers `clustered_stops` / `costly_entry_cap`, candidates off / 2 /
   4, applied to the active risk rule through `risk_settings.update_risk_settings` with the governance action
   `entry_cap_update` (entity `risk_rule`).
4. **Book-level experiment path.** `plan_experiments` sends a book template to one target, `book`, whichever
   strategy the flag came from; `run_book_experiment` replays every active replayable strategy under its own
   live setup, lets the trades compete for the live slots under the current book policy (live caps, the
   stop-out cooldown and the current cap) and under each candidate cap, and judges the book rows (per-trade
   expectancy of the accepted trades, total profit, the book's drawdown with each position at 1/20 of equity)
   with the same `decide` rule. Adoption writes the risk rule; the verdict (`review_changes`) counts every
   strategy's trades after the change; a revert sets the cap back to off (0). Book changes are not shadowed
   per BUY (`setup_shadow` decides only strategy gates); the two new gate templates are: `decide_candidate`
   reads the sector ETF frames and the gauges the protections loaded.
5. **Everything else.** `loss_diagnosis` proposals name the template that now tests them; the gate-cost audit
   maps `sector_gate` / `breadth_gate` / `entry_cap` reasons to `costly_*` lessons; `seed.py` gets the
   seed-style steps (`apply_sector_gates`, `apply_breadth_gates`, `apply_entry_cap`, all shipped empty) under
   the same contract as `enable_market_gates`, so a later hand-run report never overrides the engine's
   decision (a row or rule that carries the key, or has a governance row for the action, is left alone). They
   are not wired into `ensure_strategy_lineup` until a report adopts a value, so the lineup dict is unchanged.

## Coverage: every diagnosed cause and flag

| Diagnosis | Kind | Template | Protection (live) | Replay |
| --- | --- | --- | --- | --- |
| `missed_market_move` | cause | `market_gate` | `market_gate_sma` row parameter | `spy_gate_on` |
| `market_down` | flag | `market_gate` | same | same |
| `missed_sector_move` | cause | `sector_gate` (new) | `sector_gate_sma` row parameter | `sector_gate_on` (new) |
| `missed_earnings` | cause | `earnings_blackout` | `earnings_blackout_sessions` | `earnings_gate_on` |
| `high_vix_entry` | flag | `vix_gate` | `vix_gate` / `vix_level` | `vix_gate_on` |
| `weak_breadth_entry` | flag | `breadth_gate` (new) | `breadth_gate_min` row parameter | `breadth_gate_on` (new) |
| `clustered_stops` | flag | `entry_cluster_cap` (new) | `max_new_entries_per_session` risk rule | `book_replay.allocate` |
| `premature_stop` | flag | `stop_width` | `stop_loss_pct` | `barrier_for` |
| `gave_back_gains` | flag | `time_stop` | `max_holding_sessions` | `barrier_for` |
| `held_too_long` | flag | `time_stop` | `max_holding_sessions` | `barrier_for` |
| `costly_market_gate` / `costly_vix_gate` / `costly_earnings_blackout` | gate-cost lesson | the gate's template | gate off is always a candidate | same |
| `costly_sector_gate` / `costly_breadth_gate` / `costly_entry_cap` | gate-cost lesson (new) | the new templates | same | same |
| `shadow_evidence` | shadow lesson | the named template | same | same |
| `crowded_symbol` | flag | none needed | `paper_one_position_per_symbol` is already on | n/a |
| `exit_untested` | flag | none | fixed by trading the backtested exits (PR #23) | n/a |
| `macro_event_in_hold` | flag | **none yet** | `announcement_blackout` (account-wide setting, no per-strategy knob) | `market_events` has no replay history |
| `signal_wrong` | cause | **none by design** | the signal itself was wrong; the proposal is a feature comparison or retirement | strategy discovery / scorecard |
| `news_wrong` | cause | **none by design** | news entries have their own loop (`news_discovery`) | not replayable from daily bars |
| `noise`, `unknown_context` | cause | excluded by `repeating_failures` | n/a | n/a |

So every *gate-shaped* cause and flag now has an automatic experiment; the three without one are either
already handled by a fixed rule (`crowded_symbol`, `exit_untested`), not gate-shaped (`signal_wrong`,
`news_wrong`) or lack a replayable history (`macro_event_in_hold`).

## What was not measured

No experiment ran on real history in this session: the deployed `history_dir()` is empty and the sector,
breadth and cap templates all ship *off*, so the first numbers come from the Sunday engine run once the
lessons repeat. Three limits to read them with:

* The sector gate covers only symbols `trading_universe.SECTOR_GROUPS` maps (the 45-symbol list); the
  point-in-time universe's past members have no sector mapping and replay ungated, so a sector-gate
  experiment under `replay_point_in_time_universe` judges the gate on the mapped names and the baseline on
  all of them. A membership source for the sector ETF holdings would close this (noted as not done in PR #30).
* The breadth history is built from the members yfinance still serves (coverage 45% in 2000, 81% mean since
  2010, PR #30), so early readings are a survivor's breadth; dates below 100 priced members are left unknown
  and fail open.
* The entry cap counts *filled* entries plus the running cycle's submissions, not buy orders still pending
  from an earlier cycle; the replay caps accepted trades per day exactly. A pending-order count would make
  the live rule tighter than the replayed one, so it was left out.

## Tests

`tests/test_lesson_templates.py` (18): parsing and the ETF mapping, sector state and replay mask, the replay
gating only mapped symbols, the live sector gate blocking gated strategies and failing closed per ETF; breadth
parsing, blocked / allowed / fail-open states, the live breadth gate, the point-in-time breadth history with
its cache; the entry cap counting filled orders once and this cycle's BUYs, the book replay cap, the script
on the service; the three templates and their triggers, the plan putting the cap on the book, adoption on the
strategy row (sector, breadth) and on the risk rule (cap) with the seed-style override contract and reverts,
the breadth loader failing open, a real end-to-end replay of all three, and two cycle tests (a sector-gated
BUY withheld with the gate wiring, the cap counting the cycle's own BUYs). Two existing assertions were
widened (`current_setup` keys, the shadow view's template list).
