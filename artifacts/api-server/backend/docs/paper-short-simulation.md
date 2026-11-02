# Short-only paper research

This is an isolated historical simulator, not Alpaca paper-order execution.
Existing long-only strategies, broker ledger, approvals and live gates are
unchanged. No migration or broker credentials are needed. It is deliberately
not registered for automatic strategy promotion.

The separate `rsi2_short_bounce_v1` candidate shorts a completed-session RSI(2)
above 90 while close is below its 200-session SMA. It enters at the next open,
covers after RSI falls to 50, after five holding sessions, or on a 3% stop.
Defaults allocate at most 5% of simulated equity, rounded down to whole shares.
These are experimental rules, not evidence of profitability.

From the backend directory, with the project Python environment activated:

```sh
PYTHONPATH=. python scripts/simulate_paper_shorts.py SPY --source alpaca_iex_daily
```

Authenticated researcher API:

```http
POST /api/stock-paper/short-simulation
Content-Type: application/json

{"symbol":"SPY","source":"alpaca_iex_daily","limit":1000}
```

Optional `config` overrides the validated simulation settings. The response
contains individual short round trips, equity curve, drawdown, net P&L,
configuration and reproducible dataset/run fingerprints. Reports are returned
to the caller, not persisted or scheduled. This does not change the dashboard.

Only a single explicitly selected supported historical provider is read.
Missing sessions, unfinished daily bars, invalid OHLC data and synthetic or
unknown sources are rejected. No automatic download or synthetic backfill is
performed. Prior session data controls entries and indicator exits; stop fills
use an adverse gap open or the stop price plus modeled cover slippage.

Sale proceeds are restricted and cannot fund another position. Fees, slippage
and calendar-day borrow charges are assumptions. Dividends owed, recalls,
borrow availability and corporate actions are not modeled. Results always
report `costs_verified=false` and `execution_eligible=false`.

## Remaining Broker Work

Alpaca-connected short paper execution still requires a direction-aware broker
ledger and reconciliation, short ownership/cover reservations, absolute gross
exposure and collateral limits, borrowability checks, short protective orders,
and explicit account-specific short approval. Do not drop the existing
nonnegative-position constraint or route the simulator's shorts as SELL
signals into the current long-only broker lane.
