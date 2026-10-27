# Paper results and decision-quality audit — 2026-10-27

Scope: what did paper trading actually do, which mistakes recur, how much did
each cost, and which code paths cause them. Paper only. No orders were placed,
modified or cancelled. Live keys were not read.

## 1. Data sources and what they contain

| Source | Access | Content |
|---|---|---|
| Alpaca **paper** API (`PAPER_ALPACA_API_KEY`) | read-only GETs worked | account, 6 orders, 14 activities, 1A daily portfolio history |
| Alpaca market data (IEX daily bars) | read-only GET | SPY/AAPL/MSFT 2026-09-10 → 2026-10-27 |
| Main checkout `backend/trading_app.db` (legacy simulator, June 2026) | `sqlite mode=ro` | 8 closed `paper_trades`, 8 journal rows |
| `.paper-training/**/comparison/*.sqlite` (crypto BTC/USD simulated comparison) | `sqlite mode=ro` | per-strategy fills, fees, equity to 2026-09-16 |
| `artifacts/api-server/backend/trading_app.db`, `.paper-venue/research.sqlite` | `sqlite mode=ro` | stock-paper ledger tables empty; no stock-paper orders, fills or journal rows |
| Main checkout `output/` | listed | only UI screenshots and image edits; no trading data |

The deployed stack's own database (if a hosted Postgres exists) was not
reachable from here; the broker is the source of truth for what actually
traded, and it shows no strategy orders.

## 2. Headline numbers — Alpaca paper account (2026-09-11 → 2026-10-27)

| Metric | Value |
|---|---|
| Funding | $100,000.00 (JNLC 2026-09-11) |
| Equity now | $99,999.93; 0 open positions; unrealized $0.00 |
| Orders | 6, **all** `qa-*` round-trip probes (3 × ~$10 market buy+sell: SPY 09-29, AAPL 09-30, MSFT 10-02) |
| Strategy orders (`sp-*` stock-paper ledger) | **0** |
| Realized gross P&L | −$0.0035 (spread only: SPY 0.08 bps, AAPL 1.6 bps, MSFT 1.7 bps per round trip) |
| Fees | $0.07 (REG/CAT/TAF, each rounded up to the $0.01 minimum) ≈ 23 bps of ~$30 round-trip notional |
| Net P&L | −$0.07; win rate 0/3; payoff ratio n/a; median hold 0.2 s |
| Slippage vs decision price | n/a — probes were market orders with no decision price; scheduled orders are limit-at-reference so this becomes measurable once they exist |
| SPY buy-and-hold, same window | **+1.03% = +$1,026.57** on $100k |
| Opportunity cost vs SPY | **$1,026.64** |

Per-symbol and per-strategy breakdown is trivial: one probe strategy, three
symbols, each −$0.02/−$0.02/−$0.03 net.

## 3. Ranked mistakes by dollar cost

Costs overlap between rows (one losing lot can be stacked and oversized); each
row states its own basis.

### Actual paper account

| Rank | Mistake | Cost | Evidence |
|---:|---|---:|---|
| 1 | **Capital idle: autonomous stock path never traded** | **$1,026.64** opportunity cost vs SPY | 0 `sp-` orders in 46 days; account flat while SPY +1.03% |
| 2 | Fee drag on tiny probe orders | $0.07 | $0.01 minimum per fee type makes $10 orders cost ~23 bps |
| 3 | Instant round-trip churn (probes) | $0.07 (same dollars as #2) | by design for connectivity QA, not a strategy loss |

### Legacy local simulator (June 2026, retired code; shows what the strategies do when allowed to trade)

| Rank | Mistake | Cost | Evidence |
|---:|---|---:|---|
| 1 | **Entry booked at a price the decision never saw** | $2,567.06 | both losing SPY lots entered at 619.86 while the signal's own `latest_close` was 735.38 (−1,571 bps); exit at 420.96 — the simulator mixed price sources |
| 2 | **Oversized losers / late exit beyond the 3% stop** | $2,327.06 of the $2,567.06 | stop rule only ran at reconcile time, 6 days later, and closed at −32.1% |
| 3 | **Stacking the same symbol across strategies** | $1,283.53 | `macd_momentum` bought SPY 39 min after `model_predictive_long` did, 23 min after the operator cut that position 50% — the risk reduction was undone |
| 4 | Control-room churn | $0.00 | 6 `moving_average_crossover` SPY open/close pairs in 8 minutes (one with quantity 0) |

Realized: 8 round trips, gross/net −$2,567.06, 0 wins, payoff n/a.

### Crypto simulated comparison (BTC/USD, $100 order budget, 0.8% fee, to 2026-09-16)

| DB | Strategy | Fills | Fees | Net | Fees share of loss |
|---|---|---:|---:|---:|---:|
| comparison/baseline | trend | 16 | $12.68 | −$14.68 | 86% |
| comparison/baseline | mean_reversion | 9 | $7.16 | −$7.05 | 102% |
| comparison/baseline | buy_hold | 1 | $0.79 | −$6.36 | 12% |
| comparison/stress | trend | 16 | $15.81 | −$19.38 | 82% |
| comparison/stress | mean_reversion | 9 | $8.92 | −$9.70 | 92% |
| forward-rf champion | trend / mean_reversion | 6 / 7 | $4.76 / $5.58 | −$5.56 / −$4.69 | 86% / 119% |

Mistake: **signals that trade below round-trip cost** — fees are 82–119% of the
rule-based strategies' losses; the cost-aware ML arm (`cost_ml`) correctly
stayed flat. Both trend and mean reversion did worse than buy-and-hold, and
the extra loss is almost all fees.

## 4. Code paths behind the recurring stock mistakes

`app/services/stock_paper_autotrader.py` (the only autonomous stock order path)
had four defects that reproduce the legacy simulator's losing patterns as soon
as the launch gates open:

1. **Pyramiding on persistent state signals.** `moving_average_crossover` and
   `macd_momentum` return BUY for as long as the state holds. The idempotency
   key is per session, so every session added a new 1%-of-equity lot until the
   10% symbol cap stopped it. Neither `approve_trade` (counts positions, not
   lots) nor the ledger (`current is None` check) blocks adding to a held symbol.
2. **Same-cycle buy and sell of one symbol.** Strategies were processed in id
   order. MA=BUY and MACD=SELL on SPY reserved both a buy and a sell in one
   cycle, paying the spread twice for no change in exposure.
3. **Cross-strategy liquidation.** Any strategy's SELL sold the *entire*
   position, including lots another strategy opened, breaking per-strategy
   attribution and exiting positions on a signal that never opened them.
4. **No price-based exit.** The governed path had no stop at all; a loser was
   held until a slow state signal flipped — the "late exit / oversized loser"
   pattern that cost $2,327 in the legacy data.

## 5. What was fixed

`artifacts/api-server/backend/app/services/stock_paper_autotrader.py`

- Two-phase cycle per symbol: observe every strategy first, then act with
  **exits before entries**. A submitted or pending exit suppresses same-cycle
  entries for that symbol.
- BUY is skipped when the strategy already holds the symbol (net attributed
  fills) or has a pending buy for it — **no pyramiding on a persistent signal**.
- SELL exits **only the selling strategy's attributed quantity**. A strategy
  that holds nothing in the symbol cannot liquidate other strategies' lots.
  If attribution is unknown (manual/imported fills), it falls back to the full
  position so risk reduction stays available.
- **Protective stop**: if the price is at or below the average entry by
  `position_stop_loss_pct` (risk rule; default 3%, matching the retired
  simulator; 0 disables), the cycle reserves a full-position sell through the
  same reservation/dispatch gates (`scheduled-paper:<day>:<sym>:protective_stop:sell`)
  and skips every other action on that symbol in that cycle. It only reduces
  exposure; no gate was loosened.

`artifacts/api-server/backend/scripts/report_paper_mistakes.py` (new, rerunnable)

- GET-only Alpaca paper pull (`--from-alpaca --env-file … --save-snapshot`),
  offline `--snapshot`, legacy `--legacy-sqlite`, crypto `--comparison-sqlite`;
  all SQLite opened read-only.
- FIFO round trips, fee allocation by daily notional, win rate, payoff, hold
  time, entry slippage vs decision (limit) price, per-symbol/per-strategy
  tables, SPY benchmark and opportunity cost, and a dollar-ranked mistake list
  (fee drag, beyond-stop losers, churn, stacked entries, opening-minutes
  entries, fills far from decision price, losing strategies).
- Strategy attribution via `--strategy-map` or the `sp-`/`qa-`/`lb-`
  client_order_id prefixes (`lb-` on paper is flagged).

Reproduce this report:

```
cd artifacts/api-server/backend
python scripts/report_paper_mistakes.py --from-alpaca --env-file "<path>/.env" \
  --legacy-sqlite "<main>/backend/trading_app.db" \
  --comparison-sqlite "<main>/.paper-training/comparison/baseline.sqlite"
```

## 6. Tests

- `tests/test_stock_paper_autotrader.py`: 10 new tests (opposing signals,
  pyramiding, duplicate pending buy, non-owning sell, owned-quantity sell,
  unattributed fallback, holdings netting, stop fires/blocks re-entry, stop on
  HOLD cycles, stop inside budget or disabled). The 7 non-stop tests fail on
  the previous implementation.
- `tests/test_report_paper_mistakes.py`: 5 tests.
- Related suites (`test_stock_paper_*`, `test_paper_*`, `test_risk*`,
  `test_stock_learning_cycle`, `test_celery_app`, `test_migrations`,
  `test_monitor_security`, `test_exact_paper_session_bounds`): 377 + 176 passed,
  0 failed.

## 7. What remains / not verified

- **Why the autonomous path placed zero orders** is not established. Likely
  candidates: launch-admission gates (`evaluate_launch_admission_prerequisites`),
  no strategy with approved non-legacy evidence, or the scheduler not running
  on the deployment. The hosted database and worker logs were not reachable.
  This is the largest dollar item ($1,027 vs SPY) and needs an operator look.
  No gate was loosened to "fix" it.
- No real strategy trades exist, so win rate, payoff, holding time and
  slippage for real strategies cannot be measured yet. Rerun the script once
  `sp-` orders exist, with `--strategy-map` built from `stock_paper_orders`
  (client_order_id → strategy_type).
- The protective stop uses one attempt per session per symbol (idempotent key);
  if that order is cancelled, it retries the next session, not the same day.
  It reads `average_entry_price` from the reconciled broker position.
- The crypto comparison strategies (trend, mean reversion) need a cost gate or
  cooldown. They were not changed, because their configs are fingerprinted
  research benchmarks and a change would invalidate comparability.
- `trade_scorecard.py` still scores only the retired `PaperTrade` table; the
  new script is the place to score real paper trades.
- The legacy losses ($2,567) come from a simulator that is now blocked
  (`LEGACY_SIMULATOR_BLOCK`). The current ledger already rejects reference
  prices more than 2% from the latest persisted bar, which addresses the price
  mismatch.
