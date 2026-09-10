# Offline research simulator foundation

`services/research_simulator.py` is a pure single-instrument long-only simulator.
Supply ordered unique timezone-aware OHLCV bars with an explicit boolean
`tradable`, plus one binary target decision per close. Decision t first acts at
open t+1. Final-bar decisions never fill. A zero decision exits; a one decision
buys available allocation capacity, with repeated partial fills allowed.

Cash/quantity accounting uses Decimal. Fees apply to executed notional. Spread
is the full quoted spread in basis points (half on each side); slippage and
half-spread worsen entry/exit prices. Orders respect available cash, long-only
quantity, allocation <=100%, lot size, minimum notional, and maximum fraction
of bar volume. Nontradable/zero-volume bars do not fill. Marks use close;
unclosed positions remain marked, without a fictitious terminal liquidation.

Results include fill and completed-round-trip counts, fees/spread/slippage,
cash, quantity, equity history, return and peak-to-trough drawdown. Decimal
values must be serialized deliberately by downstream callers. Cash and
buy-and-hold benchmarks run through the same execution assumptions. Buy-and-hold
fixes a target quantity at its initial eligible open and accumulates only that
quantity through partial fills, constrained by remaining cash. It does not add
to its target after price declines or continuously rebalance.

This is the WP12 foundation, not a certified execution simulator. Callers must
obtain official exchange calendars and trustworthy bars/decision timestamps.
There is no stochastic queue, intrabar path, order-book depth, corporate-action
accounting, leverage, borrow, or latency beyond the next-bar rule. Full-bar
volume cannot increase next-open capacity: the preceding completed bar supplies
the liquidity estimate, and realized execution-bar volume only reduces it.
Actual zero-volume/nontradable bars do not fill. These retrospective constraints
are simulation assumptions, not opening-liquidity guarantees.
This approximation must be stress-tested before qualification use. No broker
calls, promotion, or live authorization exist in this module.
