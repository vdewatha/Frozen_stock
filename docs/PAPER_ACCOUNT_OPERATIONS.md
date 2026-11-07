# Paper account operations

The application is paper-only. `ALLOW_LIVE_TRADING` must remain `false`, and
the paper broker must be `alpaca_paper`.

## Free paper market-data path

When Tradier production timesales is unavailable, a live-disabled Alpaca paper
deployment may use the free IEX feed:

```text
ACTIVE_PAPER_BROKER=alpaca_paper
ACTIVE_MARKET_DATA_PROVIDER=alpaca_iex
INTRADAY_BACKFILL_CHUNKS_PER_CYCLE=8
```

This path uses authenticated Alpaca IEX one-minute bars, preserves `provider`
and `feed_class=iex` provenance, requires complete regular-session coverage,
and remains execution-ineligible whenever live trading is enabled. It does not
convert IEX observations into SIP data and does not authorize live orders.
The backfill chunk setting only increases the number of bounded one-hour
requests a worker may process per cycle; it never fills absent intervals.

## Cost evidence

`ALPACA_PAPER_ZERO_COMMISSION_CONTRACT` defaults to `false`. Enabling it is a
research assumption for an Alpaca activity-v2 account only. It reports that
missing fill commissions may be modeled as zero, while keeping account-level
fee events, spread, slippage, and regulatory costs visible as unknown. It does
not set `costs_known`, authorize execution, or authorize live trading.

## Existing legacy account

Run the activity-contract upgrade script without arguments first. It performs
two read-only broker snapshots and prints a sanitized preflight. It refuses
accounts with changing state, open positions, nonterminal orders, identity
mismatches, or unresolved accounting review.

Only after the preflight is passing may an operator explicitly apply it:

```text
python -m scripts.upgrade_stock_paper_activity_contract \
  --apply --confirm UPGRADE_ALPACA_ACTIVITY_CONTRACT_V2
```

The upgrade makes a local v2 observed baseline and records an audit event. It
does not submit, cancel, or replace any broker order. Costs remain unverified
after the upgrade and the reconciliation gate must pass again.

## Research qualification

Qualification is separate from execution admission and requires an identified
reviewer:

```text
python -m scripts.qualify_paper_research_venue --reviewer REVIEWER_NAME
```

The command does not activate paper execution. Activation still requires the
separate, distinct authorizer already enforced by the service layer.

Tradier sandbox is not a qualification fallback because its account-history
support is insufficient for this accounting contract. A fresh Alpaca paper
account with newly issued paper credentials is the cleanest path when an
existing account cannot pass the upgrade preflight.

## Research and execution universes

The paper stack deliberately separates these universes. It researches
`AAPL`, `MSFT`, `QQQ`, and `SPY`, but the default execution universe is
`AAPL`, `MSFT`, and `SPY` while QQQ has incomplete IEX minute coverage. QQQ
continues collecting real research observations and can be returned to the
execution universe only after its feed is complete. Missing bars are never
synthesized, and this setting does not clear
accounting, risk, or authorization gates.

## Self-healing

`self_healing_job` runs every 15 minutes (07:00-20:45 New York time, weekdays)
on the `learning_fast` queue and fixes the routine stalls that used to wait
for a person, each with a cooldown and a daily cap: a missing meta-label model
(enqueues the retrain off hours), traded symbols with no fresh minute bar
while SPY is current (re-runs the backfill for those symbols inside the
existing repair budget) and learning scopes whose worker lease expired
without a result (requeues them). Past a cap it raises a warning instead of
acting. A starved fast lane, a heartbeat pause after a publish and
monitoring pauses are reported only. Every action is a `self_healing` audit
row; `GET /stock-paper/self-healing` (viewer) shows the cases, budgets and
latest actions, and the weekly report's `changes` section narrates them with
the governance, experiment-engine and gate-cost rows. It never touches
orders, exits, the kill switch, risk settings or the resume logic. Switch
with `self_healing_enabled`. See `reports/self-healing-20261106.md`.

## Setup shadow: every candidate gate value earns a forward record

An adopted gate change used to be judged only after 20 live trades on the new
setup, and the values the engine did not adopt earned nothing. Now every
paper cycle records, for each strategy with a BUY signal, what each candidate
value of each gate template (market gate off/10/20/50, VIX below 20/25/30/off,
earnings blackout 0/10/20) would have decided next to the live decision, from
the states the cycle already loaded (one `setup_shadow` audit row per cycle
whose picture changed; no broker call). `setup_shadow_job` (17:25 New York
time, weekdays, `learning_fast`) scores each decision's hypothetical trade from
the next open under the strategy's tested exits, the same scoring the gate-cost
audit uses, and stores the per-candidate record (forward trades, expectancy,
win rate, delta against the live value on the same decisions) as a
`setup_shadow_score` row. `GET /stock-paper/setup-shadow` (viewer) shows the
table; the learning scoreboard has a `setup_shadow` section. The experiment
verdict carries the alternatives' record and reverts a change early when an
alternative beats the adopted value by 10 bps per trade over at least
`experiment_verdict_min_trades` overlapping decisions; a candidate that beats
the live setup becomes a `shadow_evidence` lesson for the weekly engine run,
replayed and adopted only under the unchanged pre-registered rule. Paper only;
nothing here places, blocks or exits an order. Window:
`setup_shadow_lookback_sessions` (120). See `reports/setup-shadow-20261106.md`.

## Engine audit: expected vs realised, and the calibration factor

The experiment engine adopts changes on its own, so `engine_audit.py` keeps
it honest. For every adopted change it puts the replay's promised gain (adopted
minus baseline expectancy, 2020+ window) next to the realised gain (forward
expectancy after the change minus the last trades before it, or the adopted
value against the value it replaced over the same shadow decisions while the
live trades are too few), the shortfall and the forward trade count. Across
all experiments it reports adoptions, verdict counts, the median
realised-over-expected ratio with a bootstrap interval over the judged changes
and a Bonferroni-style share of adoptions expected by luck. When the median
ratio over at least 5 judged changes is below 0.5 the Saturday job
(`engine_audit_job`, 14:30 New York time, `learning`, after the weekly
report) stores an `engine_audit` row with a calibration factor in [0.5, 1.0];
the Sunday engine run multiplies it into each candidate's expected gain and
requires the deflated gain to cover the optimism's share of the 10 bps revert
margin, so a candidate must beat the baseline by more. The factor returns to
1.0 once the median recovers above 0.8. `GET /stock-paper/engine-audit`
(viewer); the weekly report and the learning scoreboard each have an
`engine_audit` section. Paper only; audit rows only. See
`reports/engine-audit-20261106.md`.

## Point-in-time replay universe

The experiment engine and strategy discovery replay daily history over the
S&P 500 as it stood on each date, not over today's survivor list: every past
member whose history yfinance still serves is loaded, and a single stock can
only be entered while it was a member (index and sector ETFs always).
Membership comes from the MIT-licensed `fja05680/sp500` start/end file,
cached monthly with its coverage under `history_dir()/universe/`
(`app/services/point_in_time_universe.py` documents the files). The first
run with the setting on downloads every past member's bars once (about 20
minutes), later runs refresh bars older than a week. Switch with
`replay_point_in_time_universe`; `scripts/run_survivorship_check.py` measures
the bias and holds the rule that decided the default. See
`reports/survivorship-20261106.md`.

## Exploration budget: learning lots go where evidence is scarcest

Learning-sized trades (0.25% of equity with the one-share floor, from
candidate and research strategies and from news-found symbols) no longer go
wherever a signal fires. Each paper cycle builds an evidence map
(`app/services/exploration_budget.py`): per learning strategy, per regime
cell, per setup-shadow candidate and per watched experiment change, the
forward trades so far, the trades still needed to reach that item's evidence
minimum and the Wilson half-width of its win rate. Every eligible learning
BUY gets a priority (trades needed, weighted by the candidate's backtest
expectancy, discounted by that strategy's lots already in flight) and the
session's budget, `exploration_budget_fraction` of equity (1%), is allotted in
priority order; the rest are skipped with the reason `exploration_budget` in
the BUY outcome. An item with fewer than half its minimum trades and a
positive backtest expectancy may trade up to `exploration_lift_max` (2x) the
learning size, never above the scheduled size or any ledger cap; the
multiplier and its reason are recorded in the signal's `features.exploration`
and the ledger admits a lifted lot only from that record. Scheduled entries,
exits and caps are untouched. `GET /stock-paper/exploration-budget` (viewer)
shows the map, today's budget and what each cycle took and skipped; the
learning scoreboard's `exploration` section lists trades until the next
verdict per watched item. Paper only. See
`reports/exploration-budget-20261106.md`.

## Drift-triggered retraining

A persistent `model.*` monitoring breach (prediction-distribution drift,
input-feature drift, calibration or realised-performance degradation) starts
one governed learning cycle with trigger `drift` at once, instead of waiting
for the 18:30 calendar retrain. The cycle is the normal one (fresh holdout,
challenger, validation, forward paper trial, promotion gates); the trigger
only creates it, with the monitoring snapshot id, the breach keys and the
breach episodes in the cycle's lineage. `drift_retrain_job` (every 15 minutes,
`learning`) applies the guard rails: one open drift cycle at a time, a
`drift_retrain_cooldown_hours` (48) cooldown after a cycle ends, exponential
back-off (`drift_retrain_backoff_factor`) after repeated failures, a breach
episode a promoted cycle already answered never re-triggers,
`drift_retrain_max_per_day` (1), and cycle creation only inside a regular
session (open + 15 min to close − 30 min: the preflight needs the live
verified feed and the dataset must be captured on its cutoff day). The
operator pause of scheduled learning pauses it; `drift_retrain_enabled` turns
it off. `GET /stock/learning-cycles/schedule-control` carries the state under
`drift_retraining`; the ledger is the `drift_retrain` audit rows. See
`reports/drift-retraining-20261106.md`.

## Exploration lane (liquid point-in-time universe)

Setting `exploration_universe` (`"core"` by default) opens a learning-only
lane beyond the 45-name core list. With `"liquid_pit"`, `exploration_universe_job`
(07:20 ET, weekdays, `learning`) screens today's S&P 500 members from the
cached daily history: 20-day median dollar volume ranked into terciles (the
top `exploration_universe_terciles` admitted), a Corwin-Schultz spread proxy
under `exploration_universe_max_spread` (1%), and a price under the one-share
learning floor (a name whose one share exceeds it is skipped and logged). Of
the admitted names, only those where a broad-scope strategy's daily replica
BUYs on the last bar (at most `exploration_universe_max_candidates`, 30)
become active assets and join the cycle and the intraday feed, for
`exploration_universe_admit_hours` (30). Every lane entry is a learning-sized
exploration order, whatever the strategy's status. Scheduled (full-size)
entries stay on the core list. Lane positions use their own
`exploration_max_open` slots (40) and never count toward the core caps. Every
exploration entry is also capped per entry date: 3 per strategy and 3 per
sector (`exploration_max_entries_per_{strategy,sector}_per_date`). A held lane
name whose admission lapsed stays in the cycle for exits only. The state is
the `exploration_universe` audit rows. The exploration budget's evidence map
now counts effective trades, `m / (1 + (m - 1) rho)` per entry date, with
`exploration_effective_rho = 0.17`. On 2026-11-07 the pre-registered check
(`scripts/run_liquid_universe_check.py`) kept the lane closed: no tercile
traded within 5 bps of the core list. See `reports/liquid-universe-20261107.md`.
