# Setup shadow, 2026-11-06

Branch `claude/intelligent-brattain-c72e2f`, the owner's pick for speeding up learning after
`self-evolution-roadmap-20261106.md`.
**Question:** an adopted gate change is judged only after 20 live trades on the new setup
(`experiment_engine.review_changes`). With a handful of paper trades a week that verdict takes months, and the
candidate values the engine did *not* adopt earn no record at all. Can every candidate value earn a forward
track record from the same decisions the live setup makes?

**Goal used:** total profit after costs, as everywhere in the engine. A candidate is only ever adopted through
the engine's unchanged pre-registered rule; the shadow supplies evidence, not decisions.

## What was built

`app/services/setup_shadow.py`, paper only; nothing here places, blocks or exits an order.

1. **Alternative decisions recorded on every BUY.** At the end of each paper cycle (after the orders phase,
   next to the `buy_signal_outcomes` record), every strategy with a BUY signal, including one its own VIX or
   regime gate turned into a HOLD, gets each candidate value of each gate template
   (`experiment_engine.GATE_TEMPLATES`: market gate off/sma10/sma20/sma50, VIX off/below20/below25/below30,
   earnings blackout off/10/20) decided alongside the live value:
   * market gate from the SPY frame the entry protections already loaded (`EntryProtections.market_history`,
     the only edit to `entry_protections.py`), one `market_state` per window per cycle, fail closed without
     SPY history as live;
   * VIX from the process-cached reading of the last completed session (`vix_gate.vix_reading`), fail open
     without a close as live;
   * earnings blackout from the earnings calendar cache only (`earnings_known`): a symbol whose calendar the
     cycle has not fetched is `unknown` for the gated candidates, never fetched for the shadow.
   A live value outside the candidate set (`not_rising`, `blackout5`, `sma200`) is labelled and decided too.
   One compact `setup_shadow` audit row per cycle whose picture (session, symbol, strategy, per-template
   decisions) differs from the last row's: symbol, strategy, mode, decision price, live status and reason,
   and per template the live label, the live decision and each candidate's decision. No migration, no broker
   call, no per-symbol lookup beyond what the cycle did. The autotrader edit is the one call
   (`record_cycle_decisions(db, results, cycle_rows, now=now, protections=protections)`), wrapped like the
   outcome record so it can never fail a cycle.
2. **Hypothetical trades scored daily.** `setup_shadow_job` (17:25 ET weekdays, `learning_fast`, after the
   17:15 daily bar import) deduplicates the window's decisions to one per (symbol, strategy, session), the
   first observation of the session, and scores each as one hypothetical trade with `gate_cost.score_trades`
   (PR #22, imported, not duplicated): entry at the next session's open (the intraday opening print where
   minute bars exist), the strategy's tested exit plan over stored daily bars, `triple_barrier` costs on both
   sides. A trade still inside its horizon is `open`; a symbol without trusted bars is `unscorable`. One
   `setup_shadow_score` row per completed session holds the scored decisions (capped at 3000) and the records.
3. **Forward track record per candidate.** For each (strategy, template, candidate), against the live value
   as it is now (decisions recorded under an earlier live value are excluded, so a record never mixes two
   setups): decisions evaluated, decisions the candidate allowed, forward trades (allowed and scored), open,
   unknown, expectancy in bps net of the cost model, win rate, total profit; the live value's record over the
   same decisions; the overlap (scored decisions where at least one side took the trade), how many of those
   decisions differed, and the delta in expectancy and in total profit. The live value is scored on the same
   counterfactual trades, so the delta measures the gate decision alone, not fills and not the other gates.
   A side that took no trade over the overlap counts as 0 bps per decision. `beats_live` needs the overlap and
   the candidate's trades to reach `min_trades`, the expectancy delta to reach the margin and strictly more
   total profit, so a stricter gate never wins on sample size alone. `shadow_track_record(db, strategy_type,
   template, since=...)` reads the latest row.
4. **The engine hooks** (`experiment_engine.py`; `RULE` and `decide` untouched):
   * `review_changes` reads the shadow record of the alternatives since the adoption (decisions after the
     adoption session) and puts it in every verdict payload and every pending entry. A change is `not_helping`
     as soon as an alternative beats the adopted value by `REVERT_EXPECTANCY_BPS` (10 bps) over at least
     `min_trades` overlapping decisions, before the live trades reach `min_trades`; the verdict row says
     `on shadow evidence` with the beating candidate. The revert restores the parameters from before the
     change, as today; the alternative itself reaches the strategy only through the next point.
   * `plan_experiments` accepts a lesson that names its template: `setup_shadow.shadow_lessons` turns the best
     beating candidate per (strategy, template) into a lesson with cause `shadow_evidence`, count = overlapping
     decisions (so the cooldown's "grown by 3" means three more shared decisions), and the planner replays
     exactly that template. The weekly run gathers them next to the loss and gate-cost lessons
     (`shadow_lessons` in the run payload) and adopts only what passes the rule on the full-history replay.
5. **Where it shows.** `GET /stock-paper/setup-shadow` (viewer; `app/core/security.py`) returns the
   per-candidate table, the beating candidates, the templates and the thresholds; the learning scoreboard has
   a `setup_shadow` section. Setting: `setup_shadow_lookback_sessions` (120). Beat: `setup-shadow`.

Tests: `tests/test_setup_shadow.py` (18) cover the decisions from a fake cycle state (every candidate, unknown
states, live values outside the candidate set, the real VIX and earnings cache paths), the row written only
when the picture or the session changes, the cycle hook, scoring (dedup, open vs scored, the stored report and
both views), the track record and delta (counts, overlap, exclusion of stale live values, the beats rule), both
engine hooks (a shadow lesson plans exactly its template; the run replays it under the unchanged rule and keeps
the setup when the replay disagrees; a verdict carries the record, stays pending below the overlap, then
reverts on shadow evidence; a verdict without a ledger judges as before) and the job, queue, route-role and
setting wiring. Full backend suite green (see the PR).

## Why the live record is a counterfactual too

The live value's trades could be scored from the real fills. They are not, on purpose: a real fill differs
from the next-open model, another gate may have withheld the live BUY, and the lot's sizing and the gauges'
scaling do not apply to the candidates. Scoring both sides on the same hypothetical trade isolates the one
thing the template decides, whether to take the entry, which is what the candidates differ on. The real
paper trades still judge the live setup through `review_changes` as before.

## What it cannot measure yet

* **The first numbers.** The ledger starts with the first cycle after deploy; with 20 overlapping decisions
  needed per comparison and the 20-session horizon of the technical strategies, the first `beats_live` is
  weeks away, by design. Until then every record reads "more scored decisions before comparing".
* **Decisions the cycle never saw.** A BUY the daily-bar entry window turned into a HOLD before the cycle, or
  a strategy outside its symbol scope, produces no decision. The record covers the BUYs the live setup had to
  decide on.
* **Position-level effects.** Each decision is scored on its own; the slot it would have taken, the
  correlation with the day's positions and the sizing are not modelled. The delta is per trade, not portfolio
  profit, which is why adoption still goes through the full-history replay and its drawdown test.
* **Exit templates.** `stop_width` and `time_stop` candidates change the exit, not the entry decision; they
  are not shadowed here (the exit autopsy's post-exit path is the evidence for those).
* **Cost calibration.** The scoring is `gate_cost`'s; when the cost-calibration branch (PR #24) lands, the
  shadow inherits whatever `gate_cost.score_trades` does with it.
