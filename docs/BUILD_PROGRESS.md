# Build progress — 2026-09-04

Latest operational status: [real-time paper experiment](LIVE_PAPER_EXPERIMENT.md).
Models were trained on verified Kraken hourly observations; a capped, explicitly
nonqualifying forward paper trial is running. Positive trades are not promised.

This is an implementation record, not a claim that the 24-package plan is
complete. Live trading remains disabled. No real-money broker credentials have
been connected, no capital has been allocated, and no profitability threshold
has been demonstrated.

## Implemented and independently reviewed increments

The subsequent [priority implementation](PRIORITY_IMPLEMENTATION.md) adds durable
Kraken collection, safe version-bound shadow inference, a serialized paper ledger,
and a dry-run Freqtrade execution bridge with persistent-process launchers.
Its explicit limitations supersede the older component status below; this does
not certify unattended or live execution.

| Area | Delivered | Still required for full package acceptance |
| --- | --- | --- |
| Test harness | Backend unittest suite; frontend lint/typecheck/build; CI definitions | Hosted CI execution and broader end-to-end tests |
| Access control | Fail-closed bearer roles, route policy, memory-only frontend sign-in, rate limit, structured security logs | Individual identities, durable audit retention, distributed limiting |
| Database lifecycle | Versioned upgrades, schema validation before startup, SQLite and disposable PostgreSQL 16 tests, deployment migration ordering | Deployment-specific backup/staging adoption of existing unversioned databases |
| Trusted data | Provenance/OHLC/freshness validation, no synthetic execution/valuation/research fallbacks, bar-based realized outcomes | Provider normalization, corporate actions and instrument/session-aware freshness |
| Daily providers | UTC-stable timestamps, completed-day cutoff, response identity validation, missing OHLCV rejection | Official provider contracts, adjusted/raw normalization, calendar gap reports |
| Instrument contracts | Independently reviewed immutable asset/venue/base/quote identities, UTC candle timestamps, OHLC validation and fixed-interval completion checks | Database adoption, provider catalogs, equity sessions/holidays and missing-bar detection |
| Dashboard truthfulness | Missing performance explicitly unavailable; no placeholder fills/equity curves | Reconciled observed ledger performance aggregation |
| Features and training | Versioned allowlisted feature registry, purged chronological split, frozen data snapshots, logistic/random-forest baseline training, manifests/artifact hashes | Improved feature scaling, calibration, untouched evaluation protocol, cost-aware simulation and wider benchmarks |
| Research registry | Experimental-only immutable identity registration, integrity checks without model deserialization, idempotency and transactions; read-only authenticated API and dashboard panel | Full lifecycle transitions, artifact retention/access policies, approved inference loading |
| Qualification policy | Versioned deterministic evaluator with missing/invalid evidence rejection and human approval requirement | Authenticated observed-ledger evidence builder, monitor, minimum forward observation period |
| Offline simulator | Next-open decisions, Decimal accounting, adverse fees/spread/slippage, allocation/lot/volume limits, partial fills, fixed-target buy-and-hold and cash benchmarks | Certified calendars/liquidity models, full performance statistics, calibration/evidence integration |
| Freqtrade | Authenticated read-only dry-run health/config/status client; selected Kraken BTC/USD spot; validated public candles and disposable stopped Freqtrade 2026.8 startup | Persistent sidecar, model-bound signals, ledger imports, execution and reconciliation |

Independent reviewers requested and builders fixed migration logging interference,
subsecond timestamp loss, missing worker schema preflight, invalid qualification
counts, duplicate regimes, future evidence dates, synthetic research fallbacks,
and fabricated dashboard figures. Scoped acceptance does not waive any later
wave's integration gates.

See [training instructions](RESEARCH_TRAINING.md),
[authentication](AUTHENTICATION.md), [migrations](DATABASE_MIGRATIONS.md),
[qualification policy](QUALIFICATION.md), [instrument contracts](INSTRUMENTS.md), and [integration decisions](INTEGRATIONS.md).
The [offline simulator](RESEARCH_SIMULATOR.md) is separately documented and is not
the production paper ledger. It has no broker or promotion side effects.

## First public-data research experiment

Public Yahoo Chart BTC/USD daily history was fetched on 2026-09-04.
It was not written into the existing trading database or
represented as certified exchange data. The snapshot retains the explicit
`yahoo_chart_public_research_unverified` provenance claim.

The initial run `d7fd19f3ac9e5d236db7dff5a39aefea99920ba1ba23869374a18f419c49878a`
contains 535 training rows and 136 holdout rows for a five-observation horizon.
Both learned models performed worse than the training-prevalence baseline on
holdout Brier score (lower is better): logistic 0.253526, random forest 0.288378,
baseline 0.252727. These scores are not trading profits.

**This initial run is rejected as validation evidence.** Its local numerical
runtime emitted matrix-multiplication warnings. Artifacts are preserved for
diagnosis, not registered or promoted. The training pipeline now rejects numerical
or convergence warnings before publishing new artifacts. The warning was independently
reproduced on zero matrices under Apple Accelerate/NumPy. A separate Linux run
completed without these warnings.
The inspected holdout must not subsequently be described as untouched when tuning.

A subsequent provider review found that the old Yahoo parser converted candle
timestamps through the host's local timezone. For BTC midnight bars this shifted
dates and allowed an incomplete current UTC candle through. The parser now uses
UTC and withholds all current/future UTC daily bars. Earlier snapshots and the
diagnostic Linux reruns of those snapshots are not validation evidence.

### Corrected UTC run

After independent provider review, the corrected snapshot contains 730 completed
daily observations from 2024-09-04 through 2026-09-03. The Linux run
`871c945fb516dfe8651ef90db75eada9d6ffddf102816184727ee33fbbc21375`
completed without numerical warnings, with 535 training rows and 135 holdout rows.
Its manifest records the source claim, platform, architecture, package versions,
code hash, feature hash, data hash and artifact hashes.

| Predictor | Holdout Brier score (lower is better) |
| --- | ---: |
| Training-prevalence baseline | 0.252943 |
| Logistic regression | 0.254151 |
| Random forest | 0.290287 |

Neither learned model beats the baseline. This is an experimental result, not a
trading-return test or qualifying model. It was integrity-checked and registered
as ineligible in the **new separate** `backend/research_registry_20260904.db`.
The existing `backend/trading_app.db` was not migrated, modified, or relabelled.
The normal API database setting was not silently switched to the new database.

Artifacts are local under `backend/research_runs/`, excluded from Git. Existing
database price rows labelled `unknown` remain untrusted and were not relabelled.

## Remaining end-to-end gates

Venue decision: the user delegated the selection; Kraken BTC/USD spot with
Freqtrade dry-run is now the first paper target. No exchange account or real-money
venue has been approved. See [paper venue](PAPER_VENUE.md) for the implemented
public provider, observation configuration and disposable startup procedure.

Previous increment verification: **101 backend tests passed** in the isolated Python 3.12
environment. Frontend lint, TypeScript checks and production build pass in a
clean dependency installation; the production dependency audit reports zero
known vulnerabilities. Compose configuration validates. PostgreSQL 16 fresh
migrations, ORM schema parity, repeated upgrades, registry rollback and concurrent
registration passed in an isolated disposable container. Hosted CI and a full
browser-to-broker deployment have not been run.

Builder/reviewer loops completed for authentication, trusted-data boundaries,
migrations, features, offline training, registry/API/UI, provider corrections,
instrument contracts, qualification policy, monitoring, broker-status truthfulness
and the offline simulator. The simulator reviewer required true fixed-target
buy-and-hold and causal volume capacity; both fixes passed re-review.

1. Finish canonical provider/calendar and data-normalization contracts.
2. Establish reproducible numerical training, calibration and cost-aware evaluation.
3. Connect approved model versions to shadow execution and a reconciled paper ledger.
4. Extend the tested disposable Freqtrade instance into a persistent, reconciled
   paper service with approved version-bound signals.
5. Collect genuine forward observations across the required duration and regimes;
   do not manufacture elapsed days or backfill paper decisions.
6. Independently certify order idempotency, reconciliation, risk limits, kill switch,
   credential scopes, failure drills and operational monitoring.
7. Request explicit broker/account and capital/risk approval before any live pilot.

Positive examples, a high win rate, or a favourable single backtest cannot bypass
these gates. A model may never qualify; software completion cannot guarantee returns.
