# Development Paper Short Pilot

This isolated pilot can automatically submit real requests to **Alpaca paper**,
using simulated money, under a standing policy. It is separate from the
historical short simulator and the production long-only ledger. It does not
enable live trading or automatic strategy promotion. Installing code or running
tests does not enable broker execution.

## Autonomous Mode

There is no per-trade user/operator approval in autonomous mode. One-time
deployment setup supplies the dedicated account, symbol/exposure bounds, and
an authorization with `automated: true` (valid for at most 30 days). Existing
manual authorizations are never silently upgraded. Automation cannot renew its
own authority or turn research/synthetic data into execution-qualified data.

Set `DEV_PAPER_SHORTS_AUTOMATION_ENABLED=true` to run the dedicated scheduler,
and `DEV_PAPER_SHORTS_ENABLED=true` to allow new entries. Both are off by default.
The local stack launcher supervises `python -m scripts.run_paper_short_automation` when
automation is enabled. This worker is separate from the shared long-trading
workers, which must stay off for this account/database.

The read-only monitor is at **Portfolio > Short automation** (`/portfolio/shorts`).
It polls broker-observed reconciliation, the worker heartbeat, signal evidence,
execution feed status, waiting/block reasons, and order intents. A missing or
stale heartbeat is not presented as a running worker. No user trade controls
are needed on this page. The ordinary long-only ledger remains a separate lane.

The short-position P/L chart records cumulative gross trading P/L at each
successful reconciliation: owned short-sale fill proceeds minus owned cover
fill costs minus the broker-reported market liability of remaining shorts.
This includes closed-trade and open-position gross results, not deposits,
account funding, fees, or borrow costs. Net P/L stays unverified. Sale proceeds
alone are not profit. With no fills and a reconciled flat account, gross P/L
is genuinely zero, not evidence of a profitable strategy.

Only explicitly recorded P/L observations appear, starting when chart tracking
was added; older missing history is not filled with zeros. The chart exposes
the latest 1,000 reconciliation observations, with a New York date filter and
an inspectable recent-observations table. Gaps over 150 seconds break the line.

The scheduler automatically:

- Collects authenticated intraday data and repairs missing/stale daily research
  history using the explicitly pinned `alpaca_iex_daily` provider, not fallback.
- Reconciles signed positions and broker fills on every cycle, including outside
  regular market hours. It runs every minute with process-lifetime leadership.
- Evaluates the existing RSI(2) short-bounce strategy from the previous completed
  session: RSI >90 and price below the 200-session SMA. The full-session paper
  variant (`rsi2_short_bounce_full_session_v2`) permits new entries throughout
  the regular exchange session, normally 9:30 AM until before 4:00 PM Eastern,
  with automatic whole-share sizing. Exchange holidays and early closes apply.
  This extends entry timing only: it is not a new intraday signal model and
  the opening-only historical results do not validate this timing change.
- Prioritizes covers for RSI <=50, a 3% stop trigger, or five held exchange
  sessions. Urgent stop/time exits do not depend on daily history being present.
- Cancels stale orders after two minutes, waits for confirmed cancellation of
  partial fills, then covers only remaining owned short shares when needed.
- Retries zero-fill entry failures with a three-minute cooldown and at most three
  attempts per symbol/day. Once a symbol has an entry fill, it cannot reopen that
  day. Unknown submissions stay blocked rather than being submitted again.
- Records each cycle, signal evidence, action, and failure in the audit chain;
  `GET /short-pilot` includes the latest cycle. Failed cycles retry automatically
  without clearing risk gates. Loss of leadership stops the worker.

Pausing entries or disabling the entry flag does not stop automated covers,
including after restart. Turning off the separate automation flag stops the
whole scheduler. The stop is monitored by the app, not a guaranteed broker-side
stop fill; gaps, missing feeds, downtime, limit nonfills, and borrow availability
still matter. A 24/7 host/process supervisor is required for 24/7 operation.
Nothing here deploys the worker, establishes profitability, or makes the fixed
strategy self-modifying. Live and production gates are unchanged.

## Isolation

- Off by default: `DEV_PAPER_SHORTS_ENABLED=false`.
- Requires `ENVIRONMENT=local`, `ALLOW_LIVE_TRADING=false`,
  `PAPER_WORKERS_ENABLED=false`, `ACTIVE_PAPER_BROKER=alpaca_paper`, and the
  complete `DEV_PAPER_*` credential/account trio. Never use production/live keys.
- Requires an exclusive development paper account and pilot database. The same
  account cannot be active in this database's long-only ledger. Initialization
  of the long ledger is also refused if the pilot owns the account.
- Only one database/process deployment may own an account. Local checks cannot
  enforce ownership against a separate database or another broker-dashboard user.
  Disable other clients for this development account before authorizing it.
- Run `alembic upgrade head` on the isolated pilot database before startup. Do
  not point this at a production database or silently archive existing accounts.

## Policy Setup And Manual Compatibility

Every request uses the existing bearer authentication and separate role keys.
Do not put keys in URLs or source code. Routes are under `/api/stock-paper`:

1. Admin: `POST /short-pilot/authorize`. Payload: `symbols` (distinct subset of
   AAPL/MSFT/SPY/QQQ), `max_order_notional`, `max_gross_notional`, timezone-aware
   `expires_at`, `automated`, and a descriptive `reason`. Account must be flat
   with no open broker orders. Manual authorization lasts at most eight hours;
   automated standing policy lasts at most 30 days. Hard caps are $1,000
   per order, $3,000 aggregate, and 5% of account equity. These are ceilings, not
   suggested allocations. Choose smaller explicit bounds for a test.
2. In manual compatibility mode only, a different operator principal can use
   `POST /short-pilot/orders`. Automated-mode entries are reserved for the
   strategy worker; no user/operator submission is needed. Manual payload includes
   `symbol`, `intent` (`open_short` or `cover_short`), positive whole-share
   `quantity`, positive two-decimal `limit_price`, a unique `idempotency_key`
   (16-128 alphanumeric/underscore/hyphen characters), and `reason`.
3. `POST /short-pilot/reconcile` refreshes order states and compares signed broker
   positions with cumulative owned fills. `GET /short-pilot` reads the journal.
4. `POST /short-pilot/orders/{client_order_id}/cancel` requests cancellation.
   Reconcile until the broker confirms a terminal state before placing a cover
   against a partially filled entry. A cancellation request alone frees nothing.
5. `POST /short-pilot/pause` with `reason` stops entries. It does not cancel
   existing orders or cover positions. Covers remain available after pause,
   expiry, the entry flag being disabled, or the entry kill switch being enabled.
   Environment/account/feed/session checks still apply. Use the broker paper
   dashboard for an emergency exit if those checks or this app are unavailable.

## Execution Contract

The gateway is pinned to `https://paper-api.alpaca.markets`. Orders are DAY
limits, regular-session only, with `sell_to_open` / `buy_to_close` intent.
Only active, tradable, marginable, easy-to-borrow equities can be shorted. The
broker must explicitly report shorting enabled, trading unblocked, and at least
$2,000 equity. Fresh execution-eligible feed evidence, a completed reference bar,
the global kill switch and recovery state, broker buying power, and approved
exposure bounds are checked. Preflight expires after five seconds. A 5% sizing
buffer is applied; market moves can still alter filled/marked notional.

A database unique slot allows only **one unresolved order account-wide**,
including partially filled entries and pending covers. The intent is committed
before the POST. The same key never POSTs twice, even after a timeout/restart.
Timeouts, missing lookups, unknown states, changed broker order fields, foreign
orders, unexpected longs, and mismatched signed inventory block new orders.
An abandoned `checking` intent can be canceled locally: an atomic status claim
prevents its original process from subsequently submitting it. A `submitting`
or uncertain order is not cleared merely because a lookup returns 404. Stop the
submitting process and investigate with the broker before any manual recovery;
never delete an unresolved intent.

Only known short inventory can be covered; quantity cannot cross through zero
into a long. Broker-observed order prices and quantities are recorded, but the
pilot does **not** claim complete per-fill fees, borrow costs, verified all-in
accounting, or profitability. Its responses explicitly mark costs/accounting
unverified. Normal paper venue qualification/graduation evidence is not granted
by this development pilot and the production execution gates are unchanged.

## Validation

`python -m pytest -q tests/test_paper_short_pilot.py tests/test_paper_short_automation.py` uses disposable databases,
a fake broker, and an HTTP mock transport. No credentials or broker orders are
needed. Tests cover signed short/cover inventory, partial-fill cancellation,
concurrency, idempotency, restart/timeout recovery, malformed broker evidence,
account isolation, roles, limits, automatic entry/cover/cancel/retry decisions,
singleton leadership, data freshness, and paper-only HTTP routing. These are not
broker acceptance/fill tests; a separately authorized paper smoke trade is still
required before claiming actual account-specific execution works.

Provider contracts:
- https://docs.alpaca.markets/us/reference/postorder
- https://docs.alpaca.markets/us/docs/working-with-orders
- https://docs.alpaca.markets/us/docs/margin-and-short-selling
