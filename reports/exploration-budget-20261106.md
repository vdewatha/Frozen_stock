# Exploration budget, 2026-11-06

Branch `claude/ecstatic-bassi-4fb965`, from `self-evolution-roadmap-20261106.md`.
**Question:** learning-sized trades (0.25% of equity with the one-share floor; `paper_trading_candidate`
rows, discovered rules, news-found symbols) went wherever a signal fired. With 45 symbols and strict gates
the bot closes a few trades a week, so every forward verdict (experiment engine, setup shadow, regime records,
gate cost) waits months for its minimum trade count, and the wait is decided by whichever strategy happens to
signal most. Can the bot spend its learning budget where evidence is scarcest?

**Goal used:** total profit after costs. Learning trades buy evidence; this change decides *which* evidence
each session's tuition buys, not whether anything is adopted (the engine's pre-registered rule is unchanged).

## What was built

`app/services/exploration_budget.py`, paper only. It changes the learning-sized BUY decision of the paper
cycle and nothing else: scheduled (full-size) entries, exits, caps and the ledger's gates are untouched.

1. **Evidence map** (`evidence_map`). One item per thing the bot is learning about, each with the forward
   trades so far, the trades still needed to reach its evidence minimum, the win rate and its Wilson
   half-width (z = 1.96):
   * `strategy`: closed paper round trips per learning strategy (`trade_attribution` rows with source
     `exploration_round_trip` or `scheduled_round_trip`); minimum 30, the scorecard's
     `MIN_TRADES_FOR_EVIDENCE`. The item carries the candidate's backtest expectancy when the strategy row
     records one (discovered rules: `parameters.discovery.holdout.expectancy_bps`, then the discovery net
     expectancy; seeded rows may carry `parameters.backtest.expectancy_bps` or `parameters.expectancy_bps`).
   * `regime`: the same round trips per regime cell (`regime_allocation.cell_for_attribution`, PR #25), with
     the current cell always present; minimum `paper_allocation_min_trades` (5), the record's own bar.
   * `shadow`: each non-live candidate gate value from the stored setup-shadow scores (PR #28): its trades,
     the overlap with the live value, and the trades needed before the two are compared
     (`max(min_trades - trades, min_trades - overlap)`).
   * `experiment`: each watched (adopted, unjudged) change from `experiment_engine.adopted_changes`: closed
     trades on the strategy since the adoption, against the change's `watch.min_trades`; potential = the
     adopted setup's replay expectancy over the baseline's.
   * `news`: closed round trips on every news-found symbol (admitted or held), against 30.
   Every read runs inside its own savepoint and degrades to "nothing known" (a strategy without a readable
   record still appears with its full minimum needed), so the map never raises into the cycle.
2. **Budget** (`plan_exploration`, `rank_entries`). Each session may spend `exploration_budget_fraction` of
   equity (default 1%) on learning-sized lots; what this session already spent is read from the day's
   exploration BUY orders (`reserved_cash`, cancelled and rejected ones excluded). Every eligible learning BUY
   of the cycle (a candidate or research strategy's BUY with exploration enabled, or an active strategy's BUY
   on a news-admitted symbol) gets a priority:

       priority = trades still needed by the entry's items (its strategy, the current regime cell, its
                  watched changes, the news item)
                  x (1 + backtest expectancy / 100 bps, bounded to [0.5, 2])
                  / (1 + lots of that strategy already in flight)

   Lots are allotted greedily in priority order; every lot taken counts as one more in flight for its
   strategy and one fewer needed for its items, so the budget spreads across strategies instead of stacking
   one. The lot's cost is estimated with the cycle's own rounding (whole shares for the bracket, the
   `paper_min_share_round_up` and one-share floor). Entries the budget does not cover are skipped with the
   reason `exploration_budget` in the BUY outcome (`buy_signal_outcomes` sees it like any other skip); the
   cycle's `exploration_budget` summary and one `exploration_budget` audit row per changed picture per session
   record what was taken, what was skipped and the map they were ranked against.
3. **Lift** (`lift_multiplier`, `apply_lift`). A strategy item with fewer than half its minimum trades and a
   positive backtest expectancy trades up to `exploration_lift_max` (2x, hard-capped at 2) the learning size,
   so its evidence arrives sooner. The lifted quantity is capped at the scheduled size (`_quantity_for_buy`
   with the risk rule's `max_risk_per_trade`) before every later check (whole-share rounding, exposure,
   sector and lot caps). When the session budget covers only the learning size, the lift is withheld and the
   lot is taken at 1x. The multiplier and its reason are tagged on the signal (`features.exploration`); the
   ledger's `_validate_exploration_buy` admits a lot above the learning fraction only by that record, clamped
   to `[1, exploration_lift_max]`, so an untagged oversized order is refused as before.
4. **Surface.** `GET /stock-paper/exploration-budget` (viewer): the evidence map (scarcest first), today's
   budget (equity, fraction, spent, remaining), the latest cycle's taken and skipped lots with their outcomes,
   and `next_verdicts`, trades until the next verdict per watched item. The learning scoreboard gains an
   `exploration` section with the same list and the session's counts.

Settings: `exploration_budget_fraction` (0.01, in (0, 0.05]) and `exploration_lift_max` (2.0, in [1, 2]).
No migration: audit rows and signal features only.

## What changed outside the new module

* `stock_paper_autotrader.py`: one plan built after the entry limits (`plan_exploration`), the learning-sized
  branch of the BUY sizing consults it (skip with `exploration_budget`, or size with the lift under the
  scheduled cap), one `_tag_signal(..., "exploration", ...)` next to the sizing tag, and one `record_plan`
  call next to `_record_buy_outcomes`, wrapped like it. The gate wiring, scheduled sizing, exits and caps are
  untouched.
* `stock_paper_ledger.py`: `_exploration_lift(signal)` and its one use in the exploration cap.
* `config.py`: the two settings. `security.py`, `api/stock_paper.py`, `learning_scoreboard.py`: the route and
  the section. `docs/PAPER_ACCOUNT_OPERATIONS.md`: a section.

## Tests

`tests/test_exploration_budget.py` (29): the Wilson interval, the backtest expectancy lookup, the evidence
map from sample attribution, setup-shadow, adopted-change and news rows, the fallback when a record is
unreadable, the priority formula and the greedy order (a lifted lot, the in-flight discount, a spent and a
roomy session), the lift rule and its caps (scheduled size, `exploration_lift_max`, the budget), the lot
estimate's rounding, the ledger admitting a lifted lot only from the signal record, and three fake-ledger
cycle tests (the budget cut-off with the `exploration_budget` reason, the 2x lift reaching the reservation,
scheduled entries never ranked), plus the plan record, both views and the route role.

Full backend suite before the PR: see the PR description for the count.

## What this does not do

* It does not change who may explore, the lot limit (`paper_exploration_max_positions`) or the exploration
  fraction; it only chooses among the lots those rules already allow and may double one far from its
  minimum. With a 1% session budget and 0.25% lots, the budget binds only on days with five or more
  eligible learning BUYs (fewer with lifts); on most days every learning BUY is still taken.
* Shadow and experiment items are counted and surfaced but a shadow candidate's evidence comes from BUY
  decisions, which the budget does not change (a skipped lot is still a decision); they matter for the map
  and the "trades until the next verdict" list, and the experiment item adds to its strategy's priority.
* The in-flight count is an estimate from recent exploration BUY orders minus closed exploration round
  trips, not the ledger's lot accounting; it only discounts priority.
* No backtest: there is nothing to replay. The forward check is the map itself: `next_verdicts` should
  shorten faster for the items the budget favours. Revisit the priority weights when the first verdicts land.
