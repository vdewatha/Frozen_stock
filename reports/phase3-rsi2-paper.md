# Phase 3: RSI(2) / IBS into paper exploration, per-strategy exits, crossover demotion (2026-10-28)

Paper only. No broker calls were made and no orders were placed; every test uses fakes or mocks.
Inputs: `reports/strategy-scorecard-20261028.md` and `reports/meta-label-20261028.md`.

## What changed

### 1. Per-strategy exit plans

* `ExitPolicy` (`app/services/strategies/base.py`) lets a strategy declare four things: `stop_loss_pct` (the
  broker-side catastrophe stop leg), `take_profit_pct` (`0` = no leg, `None` = risk-rule default),
  `max_holding_sessions` (time stop) and `indicator_exit` (use `exit_signal`). `BaseStrategy.exit_policy()` applies
  per-row overrides from `Strategy.parameters`. An invalid override is ignored, so a bad row can never remove the
  declared stop.
* `rsi2_pullback` and `ibs_mean_reversion` declare the scorecard geometry: 10% stop, no take-profit, 10-session time
  stop, indicator exit. RSI(2) exits on close > SMA(5). IBS exits on IBS > 0.8.
* Entries: `_exit_plan(rules, price, policy)` in `stock_paper_autotrader.py` uses the strategy's percentages. With no
  take-profit, `build_exit_plan` already produces an **OTO with only the stop leg**: the Alpaca payload has
  `order_class: "oto"` and `stop_loss` but no `take_profit`. Whole-share rounding and the GTC entry are unchanged.
  Strategies without a policy keep the risk-rule 3%/3% bracket.
* App-side exits: each cycle, `_policy_exits` checks every held lot of a policy strategy for an indicator exit or a
  time stop (`app/services/strategy_execution_policy.py::evaluate_policy_exit`). A triggered exit becomes a SELL
  through the **same SELL branch** as signal exits. That branch reserves first, so every gate runs before a leg is
  touched. It then cancels the live broker legs (`live_exit_orders` → `cancel_broker_exit_orders`), defers if
  the cancel is unconfirmed, releases the reservation if a leg already filled, and only then dispatches. The
  idempotency key is the same one a SELL signal from that strategy would use
  (`{explore|scheduled}-paper:{session}:{symbol}:{strategy}:sell`), so a policy exit and a SELL signal can never
  produce two sells. A pending app-side sell suppresses the exit, and the existing reserve check (`Sell would create
  a short position`) still applies.
* Lot ownership: `_strategy_lots` walks fills in time order and **restarts attribution whenever the symbol goes
  flat**. Before this change, a single unattributed fill (an app-side sell carries no `strategy_id`) marked every
  later lot in that symbol as unattributed forever. On the live DB that would have disabled policy exits on
  SPY/QQQ, where the crossover has traded. A policy exit fires only when the current episode is attributed and the
  strategy owns quantity, or when attribution is unknown but this strategy is the episode's only buyer. It never
  liquidates another strategy's lot.
* Policy exits are evaluated even if the strategy is no longer in the cycle's tradeable set (paused, or exploration
  turned off), because an exit only reduces risk.

**Fill timing vs the backtest (by design, documented in `strategy_execution_policy.py`).** Both paths decide on
completed daily bars only. The trusted daily frame withholds today's bar, and the evaluator ignores any bar dated
today. The backtester fills an indicator or time exit at the next bar's **open**. The paper cycle acts on the first
regular-session cycle of the next session, as a limit DAY sell at the current intraday reference price, so fills
land near the open but not exactly at it. Daily import runs at 08:15 and 17:15 ET, so bar D is present before
session D+1 opens. The time stop counts NYSE sessions from the entry session (inclusive) up to today (exclusive),
which equals the backtester's `idx - entry_idx` and does not depend on the import. No policy exit fires in the
entry session.

**Protective stop unchanged.** The app-side `_protective_stop` still uses the risk-rule
`position_stop_loss_pct` (3%) and only acts on quantity that no live broker stop leg covers. An RSI(2) lot with
its 10% OTO stop leg is covered, so the 3% rule does not cut it. If the leg is missing (cancelled for an exit
that did not fill, or never armed), the stricter 3% rule applies. I did not loosen it toward the strategy's 10%.
Re-arming missing legs belongs to the concurrent execution-robustness work.

### 2. RSI(2) / IBS tradeable through governed paths

* `RESEARCH_ONLY_STRATEGY_SLUGS` is now empty. `seed.ensure_reversion_candidates` creates `RSI(2) Pullback`
  (rsi<10, SMA5 exit) and `IBS Mean Reversion` (ibs<0.1, IBS>0.8 exit) as **`paper_trading_candidate`**. It
  only creates missing rows and never edits existing ones.
* As candidates they trade only through exploration (`PAPER_EXPLORATION_ENABLED`, already `"true"` in `.replit`):
  small tagged `paper_exploration` orders with no evidence id, so they **never count toward promotion**.
  Exploration size, lot limit, min-confidence and entry protections still apply. Promotion still needs the
  normal forward-trial / reactivation evidence.
* **Per-strategy symbol scope** (new): `default_symbol_scope` on the class (SPY/QQQ/IWM/DIA for both), overridable
  per row by `parameters["symbol_scope"]` (list or comma string). The seeded rows store it explicitly, so it can
  be changed in the DB. The signal batch skips out-of-scope pairs, so no signal rows are persisted and no strategy
  compute runs for them. The cycle ignores them too. Unscoped strategies keep the full universe. All four ETFs are
  already in `PAPER_EXECUTION_SYMBOLS`.

### 3. Crossover demotion and readiness

* `seed.demote_moving_average_crossover` moves `moving_average_crossover` from `paper_trading_active` to
  `paper_trading_candidate`. It writes a hash-chained `strategy_governance` audit event (`action =
  "scorecard_demotion"`) that carries the scorecard reason and expectancy CIs. It is one-shot: it acts only while
  the row is active and has no previous demotion event, so a later governed re-activation is not undone. A fresh
  DB now seeds the crossover as a candidate.
* The crossover then continues only as exploration. Its existing open lots still exit through broker legs, the
  protective stop and its own SELL signals, which the exploration path acts on.
* **Readiness decision:** before this change, zero active strategies made "Strategy availability" a `warning`.
  That warning is not in the allowed list, so `paper_trading_allowed` became False. It would block the manual
  signal-cycle endpoint and turn the deployment monitor to `blocked`. The scheduled cycle does not consult
  readiness. Now the check is `ready` with `mode: "exploration_only"` when exploration is enabled and at least one
  active, registered candidate/research strategy exists. With exploration off, or no strategies, it stays a
  `warning`. This check only reports whether anything can trade. It admits no order, and every other gate is
  unchanged: kill switch, reconciliation, recovery, risk, feed, scheduler, reservation/dispatch and the
  exploration budget.

## Deploy notes

* **No Alembic migration and no schema change.** The current head is still `0063_stock_paper_bracket_exits`. The
  data changes (two new Strategy rows, the crossover status flip and its audit event) are applied by the
  idempotent boot-time seed (`seed_defaults` → `ensure_strategy_lineup`), which runs on every API start (production:
  the `seed-defaults` background thread). I used this pattern instead of an Alembic data migration because the
  audit log is hash-chained (`write_audit_log`) and a raw-SQL migration could not extend that chain correctly.
  Replit therefore has no prod SQL to approve for this change. After deploying, check the boot log for
  `Production default seeding deferred` (it means the seed failed and will retry on the next boot), and confirm in
  `/api` readiness that "Strategy availability" reads `exploration_only`.
* Settings: none are new. Exploration must stay enabled (`PAPER_EXPLORATION_ENABLED="true"`, already set) for
  anything to trade with zero active strategies. With `PAPER_EXPLORATION_RISK_FRACTION="0.01"` and about $100k
  equity, an entry is about $1,000, i.e. 1–4 whole shares depending on the ETF's price.
* Expect a single position per symbol at a time. A new entry is still skipped while another lot's exit legs are
  live in that symbol.

## Tests

* New: `tests/test_strategy_execution_policy.py` (22 tests). It covers policy declarations and overrides; OTO
  stop-only vs default bracket plans and the dispatched Alpaca payload; scope in the cycle and in the signal batch;
  indicator exit with reserve → cancel legs → dispatch order; time stop at exactly 10 sessions (and none at 9);
  no exit in the entry session; a persistent BUY superseded by the exit; no double sell (pending sell, or own SELL
  signal plus policy exit); never short and never another strategy's lot; sole-buyer fallback; exits for a paused
  strategy; strategies without a policy unchanged; the NYSE session count (weekend, Thanksgiving);
  completed-bar-only evaluation; attribution restart after flat; the seeding/demotion lineup (idempotent,
  re-activation respected, audit event); and readiness with zero active strategies (exploration on → ready,
  off → warning, nothing seeded → warning).
* Updated: `test_strategy_scorecard.py` (the new slugs are seeded scoped candidates; a fresh seed has the crossover
  as a candidate) and `test_stock_paper_signal_batch.py` (the unscoped fan-out test excludes the scoped slugs).
* Results: the new file has 22 passed. The related set of 14 files (autotrader, bracket exits, exploration, signal batch,
  scorecard, meta-label shadow, trading universe, entry protections, paper scorecard, ledger, readiness) has 240
  passed. The full backend suite has **1817 passed, 32 skipped, 0 failed** (6m25s).

## Known limits / follow-ups

* App-side sells still carry no `strategy_id` (`reserve_stock_paper_order` binds a strategy only for buys).
  Restarting attribution at flat makes that harmless for single-strategy episodes. Binding the sell's strategy in
  the ledger would also fix `_open_exploration_lots`, which nets exploration sells under a `None` strategy. I left
  it alone because the ledger is being changed concurrently.
* An exit that cancels the stop leg and then does not fill (a limit DAY order) leaves the lot without a broker stop
  until the next session's cycle sells it. The 3% app-side protective stop covers that gap.
* The meta-label filter stays in shadow mode. RSI(2) BUYs are scored and recorded like any other BUY.
