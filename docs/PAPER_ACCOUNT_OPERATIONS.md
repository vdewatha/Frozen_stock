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
