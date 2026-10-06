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
