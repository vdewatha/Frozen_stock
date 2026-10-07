# Paper research worker audit — October 7, 2026

Observed approximately 5:14–5:16 p.m. America/New_York (21:14–21:16 UTC).
This is a point-in-time audit, not a durability or profitability qualification.

## Runtime evidence

The active deployment is `frozen-stock-fresh` on Docker Desktop. The historical
`colima-frozen-stock` socket was absent; checking only that context would have
incorrectly suggested the application was stopped.

Celery inspection returned nine responsive workers: three learning workers and
one each for market data, research, intraday, monitoring, execution, and risk.
Each learning worker had four processes (12 total), with scope-task counters of
69, 63, and 60 respectively since worker startup. An active-task snapshot showed
eight model-predictive scopes running across all three learning containers.
These counters are execution observations, not unique experiment counts.

Database queries found all 96 combinations of 12 active symbols and eight
strategies, with zero missing combinations. Every strategy had fresh backtests
and linked experiments for all 12 symbols within the preceding ten minutes.

- Symbols: AAPL, AMD, AMZN, DIA, GOOGL, IWM, META, MSFT, NVDA, QQQ, SPY, TSLA.
- Strategies: bollinger_mean_reversion, channel_breakout, ensemble,
  macd_momentum, model_predictive_long, moving_average_crossover,
  rsi_mean_reversion, trend_pullback.

Recent learning logs confirmed successful scope completion, `paper_only: true`,
and `applied_parameters: null`. The scope implementation passes
`apply_promotions=False`. This audit did not enqueue work or change deployment
capacity; it observed already-running work.

## Data coverage and limitations

All 12 symbols have 500–501 daily bars, ending October 6. October 7 daily bars
were not yet present at observation time. Daily predictions exist for all 12;
only AAPL/MSFT/QQQ/SPY currently have realized daily predictions (one each).

Intraday collection and online learning intentionally use only AAPL, MSFT, QQQ,
and SPY. The other eight daily-research symbols have no equivalent intraday
coverage in this configured collector. Expanding this allowlist would affect
shared execution-data code and requires a separately scoped change.

| Symbol | October 7 IEX minutes | Delayed SIP minutes | Scored forward forecasts | Expired forecasts |
| --- | ---: | ---: | ---: | ---: |
| AAPL | 390 | 390 | 415 | 9 |
| MSFT | 389 | 390 | 401 | 11 |
| QQQ | 371 | 390 | 328 | 14 |
| SPY | 388 | 390 | 408 | 10 |

Forecast counts are retained totals, not October 7-only counts. All 1,552 scored
forecasts have comparator and shadow payloads. Five comparator declarations
cover online SGD, five-minute momentum, five-minute reversal, historical, and
neutral forecasts; payload presence alone does not prove economic performance.
There were no pending forecasts in this snapshot.

IEX is missing 22 symbol-minutes relative to the 390-minute session grid.
Those observations remain unknown; delayed SIP must not fill IEX gaps or qualify
real-time execution. Logs showed successful IEX polling each minute and delayed
SIP polling every five minutes after the close. Latest exchange timestamps were
3:59 p.m. Eastern. Recent polling is not evidence of new off-session market bars.

## Safety and failures

Runtime settings confirmed `allow_live_trading=False`, paper provider
`alpaca_paper`, and both research collectors enabled. The full stack is not
observation-only; it also contains governed paper execution workers. No orders,
approval changes, safety resets, or live actions were performed by this audit.

No new notification records occurred in the preceding two hours. One older open
critical deployment-monitor notification remained: complete accounting and risk
state readiness blockers. It was not cleared. The retry worker returned an empty
retry list. Missing IEX bars and expired forecasts remain visible limitations,
not evidence of a worker failure that should be bypassed.

## Validation and changes

99 tests and four subtests passed across Celery routing/scheduling, online
research, IEX collection, delayed SIP collection, and research matrix coverage.
Tests used isolated SQLite configuration and an unreachable test Redis endpoint:

```sh
PYTHONPATH=artifacts/api-server/backend DATABASE_URL=sqlite:// REDIS_URL=redis://127.0.0.1:1/15 ALLOW_LIVE_TRADING=false .local/paper-runtime/bin/python -m pytest -q artifacts/api-server/backend/tests/test_celery_app.py artifacts/api-server/backend/tests/test_online_research.py artifacts/api-server/backend/tests/test_iex_research_data.py artifacts/api-server/backend/tests/test_delayed_sip_research.py artifacts/api-server/backend/tests/test_research_matrix.py
```

An initial invocation without `PYTHONPATH` failed test collection; the corrected
invocation above passed. Three dependency warnings remained. This was focused
validation, not a full backend suite.

Corrected two stale README references from five-minute to minutely IEX polling
and documented the distinction between daily and intraday research universes.
No runtime logic change was necessary to establish current matrix coverage.
