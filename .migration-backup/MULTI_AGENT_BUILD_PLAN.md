# Multi-Agent Build Plan: From Research Models to Controlled Live Trading

## 1. Mission

Build a trading platform that can:

1. ingest trustworthy historical and current market data;
2. train reproducible predictive models without data leakage;
3. validate models with chronological, cost-aware evaluation;
4. run qualified models in shadow and paper modes;
5. promote a model to a very small live-money pilot only after deterministic gates and explicit human approval;
6. continuously monitor live behavior and automatically reduce risk or stop trading when evidence deteriorates.

The system must never interpret a short winning streak as proof that a model is safe. Promotion is based on minimum sample sizes, net expectancy, drawdown, probability calibration, operational reliability, and performance across market regimes.

## 2. Non-Negotiable Safety Rules

- The default operating mode is `research` or `paper`; live trading is fail-closed.
- Model code cannot submit broker orders directly.
- Models cannot change risk limits, approve themselves, promote themselves, or increase capital.
- Every live promotion and capital increase requires explicit human approval recorded in an immutable audit event.
- Synthetic prices may be used only in tests and demonstrations. They may never qualify a model or create paper/live orders.
- Training, validation, test, shadow, paper, and live records must be distinguishable by provenance.
- Missing, stale, untrusted, malformed, or out-of-order market data blocks new positions.
- Uncertain broker state blocks new orders until reconciliation succeeds.
- Duplicate requests must not create duplicate orders.
- A kill switch must work without the model, scheduler, or frontend.
- No leverage, derivatives, short selling, or automatic capital scaling in the first live release.
- Secrets never appear in source control, logs, model artifacts, audit payloads, or frontend bundles.
- All production mutation endpoints require authentication and role authorization.

## 3. Agent Roles

Each work package has two separate owners.

### Builder agent

- Reads this plan, repository instructions, relevant interfaces, and upstream dependencies.
- Implements only the assigned package.
- Adds or updates automated tests.
- Runs the package verification commands.
- Produces a handoff containing changed files, design decisions, test evidence, remaining risks, and migration/rollback notes.
- Does not declare its own work accepted.

### Reviewer agent

- Must not be the builder for the package.
- Reviews the complete diff and builder handoff.
- Runs relevant tests independently.
- Checks behavior against package acceptance criteria and the global safety rules.
- Reports findings by severity: `P0`, `P1`, `P2`, or `P3`.
- Requests fixes for every actionable finding.
- Approves only when no P0/P1/P2 findings remain and required evidence exists.

### Integrator agent

- Owns compatibility between accepted packages.
- Merges only reviewer-approved packages.
- Runs cross-package tests and migrations.
- Sends integration defects back to the responsible builder, followed by another independent review.

### Safety reviewer

- Reviews authentication, secrets, broker boundaries, risk enforcement, promotion logic, auditability, and fail-closed behavior.
- Can block a wave even if component reviewers approved individual packages.

## 4. Mandatory Build-Review-Fix Loop

Every package follows this state machine:

```text
PLANNED
  -> IN_PROGRESS
  -> BUILDER_COMPLETE
  -> IN_REVIEW
      -> CHANGES_REQUESTED -> IN_PROGRESS
      -> APPROVED
  -> INTEGRATION_TESTING
      -> CHANGES_REQUESTED -> IN_PROGRESS
      -> ACCEPTED
```

Rules:

1. Builder posts a handoff and stops changing the package unless fixes are requested.
2. Reviewer checks the diff, executes tests, and returns either `APPROVED` or `CHANGES_REQUESTED`.
3. If changes are requested, the builder addresses each finding and posts a response mapping findings to fixes.
4. The same reviewer re-reviews the fixes when available; otherwise a new reviewer must read the original findings.
5. Approval is invalid without recorded test evidence.
6. Integrator runs the wave-level suite after all packages in the wave are approved.
7. Integration failures return to the owning builder and repeat review.
8. No later wave may depend on code that is merely builder-complete; dependencies must be accepted.

## 5. Required Handoff Templates

### Builder handoff

```markdown
Package: WP-XX
Status: BUILDER_COMPLETE

Changed files:
- ...

Implemented:
- ...

Design decisions:
- ...

Verification executed:
- command: result

Acceptance criteria evidence:
- criterion: evidence

Migrations/configuration:
- ...

Known limitations and risks:
- ...

Rollback procedure:
- ...
```

### Reviewer report

```markdown
Package: WP-XX
Status: APPROVED | CHANGES_REQUESTED

Findings:
- [P1] file:line — problem, impact, and required correction

Tests independently executed:
- command: result

Acceptance criteria checked:
- criterion: PASS | FAIL — evidence

Safety-rule compliance:
- rule: PASS | FAIL

Final decision:
- ...
```

## 6. Branch and Change Isolation

- Use one branch or isolated worktree per package: `codex/wp-XX-short-name`.
- Do not combine unrelated packages in one change.
- Schema changes must include forward migration, downgrade/rollback strategy, and compatibility notes.
- API changes must include schema, service, client, and contract tests in the same package unless explicitly split by an accepted interface specification.
- Generated files, local databases, caches, credentials, and model binaries must not be committed unless the package explicitly defines a safe artifact fixture.
- Upstream open-source code must retain required copyright and license notices.

## 7. Technical Target Architecture

```text
Next.js Control UI
        |
Authenticated FastAPI Control Plane
        |
        +-- Governance and Promotion Service
        +-- Deterministic Risk Engine
        +-- Audit and Notification Service
        +-- Model Registry
        +-- Execution Orchestrator
        |
PostgreSQL + Redis/Celery
        |
        +-- Market Data Workers
        +-- Feature/Training Workers
        +-- Evaluation Workers
        +-- Reconciliation/Monitoring Workers
        |
Provider Adapters
        +-- Yahoo/equity data
        +-- Freqtrade crypto data and dry-run execution
        +-- Future live broker adapter
```

Models return predictions and metadata only. The execution orchestrator converts an approved signal into an order intent. The risk engine evaluates the intent. A broker adapter accepts only a signed, risk-approved intent in the currently authorized operating mode.

## 8. Delivery Waves and Parallel Work Map

Packages in the same wave may run concurrently unless a dependency is listed.

```text
Wave 0: WP-00, WP-01
Wave 1: WP-02, WP-03, WP-04, WP-05
Wave 2: WP-06, WP-07, WP-08, WP-09
Wave 3: WP-10, WP-11, WP-12, WP-13
Wave 4: WP-14, WP-15, WP-16
Wave 5: WP-17, WP-18, WP-19
Wave 6: WP-20, WP-21
Wave 7: WP-22, WP-23
```

No live broker credential is introduced before Wave 6 is fully accepted.

---

## Wave 0 — Baseline and Interface Freeze

### WP-00: Repository baseline and executable test harness

Dependencies: none

Builder tasks:

- Inventory backend, frontend, database, workers, deployment files, and current runtime versions.
- Add a backend test layout using pytest with isolated temporary SQLite and PostgreSQL-compatible test patterns.
- Add frontend lint, type-check, unit-test, and production-build commands that terminate reliably.
- Add CI stages for formatting/static checks, backend tests, frontend tests/build, migration tests, secret scanning, and dependency auditing.
- Add deterministic fixture factories for assets, candles, strategies, predictions, risk rules, and paper trades.
- Document exact local verification commands.
- Ensure CI does not contact real brokers or require real secrets.

Deliverables:

- Test and CI configuration.
- Smoke tests for `/health`, startup, database session, and frontend rendering.
- `DEVELOPMENT.md` with environment and verification instructions.

Acceptance criteria:

- Clean checkout can install and run checks using documented commands.
- Test processes terminate and return correct exit codes.
- CI fails on test, type, migration, or build failures.
- No test can submit an external order.

Reviewer focus:

- Isolation, deterministic tests, cached build contamination, environment assumptions, and accidental network access.

### WP-01: Architecture decision records and domain contracts

Dependencies: none

Builder tasks:

- Write ADRs for the control-plane/execution-plane split, paper/live mode separation, Freqtrade sidecar boundary, model registry, immutable audit events, and human promotion approval.
- Define typed domain contracts for `Instrument`, `Candle`, `FeatureSet`, `DatasetSnapshot`, `ModelVersion`, `Prediction`, `Signal`, `OrderIntent`, `RiskDecision`, `BrokerOrder`, and `ExecutionFill`.
- Specify units, decimal precision, timestamps, timezones, identifiers, enums, and JSON serialization.
- Define canonical symbol mapping between equities (`SPY`) and crypto pairs (`BTC/USDT`).
- Define idempotency and correlation identifiers across prediction, signal, risk decision, order, fill, and position.

Deliverables:

- ADR directory.
- Contract specification with examples and invalid examples.
- Initial shared schemas without behavior changes.

Acceptance criteria:

- Every future package can reference stable contracts.
- Money and quantity fields avoid binary floating-point at persistence and execution boundaries.
- All timestamps are timezone-aware UTC.
- Operating mode is explicit in every order-related contract.

Reviewer focus:

- Ambiguous fields, unsafe defaults, missing state transitions, precision loss, and mode confusion.

## Wave 1 — Repair the Foundation

### WP-02: Authentication and authorization

Dependencies: WP-01 accepted

Builder tasks:

- Add production-safe authentication.
- Define roles such as `viewer`, `researcher`, `operator`, and `administrator`.
- Protect all mutation endpoints; protect sensitive read endpoints containing trades, audit records, configuration, or model metadata.
- Require elevated authorization for kill-switch disable, strategy resume, risk-setting changes, promotions, and future live actions.
- Add CSRF protection if browser cookies are used; otherwise use securely stored bearer credentials with a documented threat model.
- Add rate limiting and structured security audit events for failed and successful privileged actions.
- Disable or protect interactive API documentation in production.

Acceptance criteria:

- Anonymous users cannot mutate state.
- Each role has contract tests proving allowed and forbidden actions.
- Privileged actions record actor, request correlation ID, reason, and result.
- Secrets/tokens never appear in logs or responses.

Reviewer focus:

- Authentication bypass, insecure defaults, horizontal/vertical privilege escalation, CSRF, token leakage, and production configuration.

### WP-03: Repair database migrations and startup lifecycle

Dependencies: WP-00 accepted

Builder tasks:

- Repair the duplicate `market_prices` provenance migration without invalidating already deployed databases.
- Add migrations for all ORM tables and constraints, including decision journals and scanner jobs.
- Stop using `Base.metadata.create_all()` as the production migration mechanism.
- Add explicit migration execution to deployment flow.
- Add uniqueness, foreign-key, check, and index constraints required by domain contracts.
- Test fresh upgrade, upgrade from representative prior schema, downgrade where safe, and application startup after migration.

Acceptance criteria:

- `alembic upgrade head` succeeds on an empty database.
- Upgrade succeeds from every documented supported schema state.
- Alembic schema and ORM metadata have no unexplained drift.
- Production startup refuses to run against an incompatible schema.

Reviewer focus:

- Data loss, non-idempotent deployments, SQLite/PostgreSQL differences, partial upgrades, and missing constraints.

### WP-04: Trusted market-data execution boundary

Dependencies: WP-01 accepted

Builder tasks:

- Remove synthetic fallback from candidate qualification, model scoring, paper execution, valuation, and reconciliation.
- Mark synthetic generators as test/demo-only.
- Validate requested instruments against the active instrument registry.
- Enforce data-source allowlists, freshness thresholds, minimum history, monotonic timestamps, positive prices, OHLC consistency, and duplicate handling.
- Return explicit blocked results for missing/untrusted data.
- Make readiness checks instrument-specific for every requested action.

Acceptance criteria:

- No order intent can be produced from synthetic or untrusted prices.
- Arbitrary unregistered symbols are rejected.
- Stale/missing candle tests fail closed.
- Audit events identify the rejected instrument and data-quality reason.

Reviewer focus:

- Alternate paths that still use generated data, source-label spoofing, boundary timestamps, and asset-specific readiness.

### WP-05: Unified readiness and fail-closed state machine

Dependencies: WP-01 accepted

Builder tasks:

- Define a single readiness result consumed by UI, schedulers, paper execution, and future live execution.
- Eliminate disagreement between `paper_trading_allowed` and execution behavior.
- Separate research, shadow, paper, and live readiness.
- Treat unknown exceptions, timeouts, missing configuration, unresolved broker state, and database inconsistency as blocked for execution.
- Include model, instrument, data, risk, scheduler, broker, and notification readiness.

Acceptance criteria:

- The displayed readiness value exactly matches execution permission.
- Property/parameterized tests cover all check combinations.
- Warnings have explicitly documented allow/block semantics by operating mode.
- Any unknown state blocks live execution.

Reviewer focus:

- Boolean drift, warning semantics, race conditions, cached readiness, and bypass endpoints.

Wave 1 integration gate:

- Full test suite passes.
- Fresh and upgrade migrations pass.
- Security reviewer finds no open P0/P1/P2 issues.
- A scripted adversarial test cannot trade with synthetic data, bypass authentication, or execute while readiness is false.

## Wave 2 — Data and Reproducible Modeling

### WP-06: Canonical instruments and calendars

Dependencies: WP-01, WP-03 accepted

Builder tasks:

- Implement instrument persistence and symbol aliases.
- Support equities and crypto pairs without conflating their calendars or currencies.
- Store venue, asset class, base/quote currency, timezone, candle timeframe, price precision, quantity precision, and active status.
- Implement equity sessions/holidays and continuous crypto calendars.
- Add conversion utilities and reject ambiguous symbols.

Acceptance criteria:

- `BTC/USDT` and `SPY` resolve unambiguously.
- Calendar tests cover weekends, holidays, daylight-saving transitions, and continuous markets.
- Position valuation uses the correct quote currency and precision.

### WP-07: Market-data provider framework

Dependencies: WP-04, WP-06 accepted

Builder tasks:

- Define provider interface for historical downloads and incremental refresh.
- Adapt the existing Yahoo source.
- Add retry, backoff, timeout, rate-limit, and circuit-breaker behavior.
- Persist raw provider identity, retrieval time, requested range, returned range, and validation outcome.
- Make imports idempotent.
- Detect revisions and gaps without silently rewriting provenance.

Acceptance criteria:

- Re-importing the same data does not duplicate candles.
- Provider failures do not relabel old data as fresh.
- Data-quality failures prevent publication to the trusted dataset.
- Provider contract tests run from fixtures without the network.

### WP-08: Dataset snapshot and feature registry

Dependencies: WP-03, WP-06 accepted

Builder tasks:

- Create immutable dataset snapshots defined by instruments, range, source versions, row hashes, and quality report.
- Implement a feature-generator registry inspired by the MIT-licensed Intelligent Trading Bot design.
- Separate input features from future-derived labels.
- Version every feature definition and its parameters.
- Guarantee identical offline and online feature computation.
- Add missing-value, warmup, normalization, clipping, and outlier policies.
- Add tests proving features at time `t` cannot access data after `t`.

Acceptance criteria:

- Dataset and feature hashes are reproducible.
- Feature parity tests pass between batch and single-latest-row execution.
- Leakage tests intentionally fail for a known bad fixture.
- License attribution is included for any copied or adapted MIT code.

### WP-09: Model registry and artifact storage

Dependencies: WP-03, WP-08 accepted

Builder tasks:

- Persist model family, parameters, random seed, dataset snapshot, feature set, label definition, training code version, metrics, status, and artifact checksum.
- Store model binaries outside ordinary relational rows using a pluggable artifact store; keep immutable metadata in PostgreSQL.
- Define statuses: `training`, `failed`, `experimental`, `validated`, `shadow`, `paper`, `live_pilot_candidate`, `live_pilot`, `paused`, and `retired`.
- Enforce legal transitions and actor/reason requirements.
- Verify checksum before loading a model.

Acceptance criteria:

- A prediction references exactly one immutable model version.
- Replacing or corrupting an artifact is detected.
- Illegal status transitions fail transactionally and are audited.
- Deleting a local artifact cannot erase historical model metadata.

Wave 2 integration gate:

- A trusted fixture dataset becomes an immutable snapshot.
- Features are calculated identically in training and online simulation.
- A registered experimental model can be reproduced from its metadata.

## Wave 3 — Training and Evaluation

### WP-10: Training worker

Dependencies: WP-08, WP-09 accepted

Builder tasks:

- Move training to an isolated worker queue.
- Implement deterministic baseline models: logistic regression and a tree-based model.
- Add fixed seeds, bounded resources, timeouts, cancellation, and failure notifications.
- Separate training data, tuning/validation data, and untouched test data chronologically.
- Persist all artifacts and metrics.
- Never expose the final test set during tuning.

Acceptance criteria:

- Same inputs and seed produce equivalent artifacts and metrics.
- Training failure leaves no model in an executable state.
- Worker resource limits prevent unbounded memory/CPU use.
- Baseline model results are always available for comparison.

### WP-11: Walk-forward and leakage evaluation

Dependencies: WP-08, WP-10 accepted

Builder tasks:

- Implement expanding and rolling chronological folds.
- Purge/embargo samples when label horizons overlap split boundaries.
- Add lookahead, target leakage, duplicate timestamp, future normalization, and survivorship-bias checks.
- Evaluate across market-regime slices and instruments.
- Preserve an untouched final holdout.

Acceptance criteria:

- Fold definitions are stored and reproducible.
- Overlapping labels cannot leak across folds.
- Deliberately leaky strategies are rejected by tests.
- Evaluation reports show per-fold and aggregate uncertainty, not only averages.

### WP-12: Cost-aware simulator and benchmark suite

Dependencies: WP-06, WP-07 accepted

Builder tasks:

- Model commissions, spread, slippage, latency, minimum notional, precision, rejected orders, partial fills, and market-session constraints.
- Prevent same-candle impossible entry/exit assumptions.
- Add benchmarks: cash, buy-and-hold, and simple deterministic strategies.
- Calculate net expectancy, return, drawdown, Sharpe/Sortino with documented assumptions, profit factor, turnover, exposure, and trade counts.
- Add scenario tests with adverse costs.

Acceptance criteria:

- All reported performance is net of modeled costs.
- Simulator never spends unavailable cash or sells unavailable inventory in long-only mode.
- Results reconcile from fills to positions, cash, and equity.
- Benchmark comparisons use identical dates and capital assumptions.

### WP-13: Probability calibration and evaluation report

Dependencies: WP-10, WP-11 accepted

Builder tasks:

- Add Brier score, log loss, reliability curves, calibration slope/intercept, and confidence-bin sample counts.
- Support calibration fitted only on validation data.
- Measure stability by time, instrument, and regime.
- Generate a machine-readable evaluation report used by promotion rules.

Acceptance criteria:

- Calibration never uses final holdout outcomes during fitting.
- Reports include sample size and uncertainty.
- Models that are profitable only at uncalibrated extreme probabilities are flagged.

Wave 3 integration gate:

- One baseline and one candidate model complete the full reproducible pipeline.
- Results include costs, benchmarks, leakage checks, calibration, regime slices, and untouched holdout performance.
- No model is promoted automatically.

## Wave 4 — Governance, Shadow Mode, and Paper Execution

### WP-14: Deterministic promotion policy

Dependencies: WP-09, WP-11, WP-12, WP-13 accepted

Builder tasks:

- Implement versioned promotion policies as data plus deterministic code.
- Initial research-to-shadow gates should include minimum sample size, positive net expectancy, maximum drawdown, profit factor, calibration, benchmark comparison, regime coverage, and no critical data/leakage failures.
- Store every gate input, threshold, result, policy version, actor, and reason.
- Require human approval where specified.
- Add demotion and retirement rules.

Suggested starting thresholds, configurable and subject to review:

- At least 100 completed out-of-sample trades.
- Positive net expectancy after costs.
- Profit factor at least 1.20.
- Maximum drawdown no greater than 10%.
- Better risk-adjusted result than the selected benchmark.
- Passing leakage checks and acceptable probability calibration.
- Evidence from more than one market regime.

Acceptance criteria:

- Changing a threshold creates a new policy version.
- Models cannot approve themselves.
- A single failed mandatory gate blocks promotion.
- Tests cover every transition and threshold boundary.

### WP-15: Shadow execution

Dependencies: WP-05, WP-09, WP-14 accepted

Builder tasks:

- Run scheduled predictions on live trusted data without producing broker orders.
- Record the exact data available at decision time, prediction latency, intended entry/exit, and later outcome.
- Prevent backfills from being counted as real-time shadow decisions.
- Monitor prediction gaps, feature parity, model-load failures, and data delays.

Acceptance criteria:

- Shadow mode cannot reach an execution adapter.
- Every shadow prediction has a decision-time timestamp and data cutoff.
- Late/backfilled predictions are identified and excluded from qualification.

### WP-16: Internal paper ledger and risk engine hardening

Dependencies: WP-05, WP-12, WP-14 accepted

Builder tasks:

- Make order intents and risk decisions transactional and idempotent.
- Enforce position, symbol, strategy, gross exposure, daily loss, total drawdown, consecutive loss, and stale-data limits.
- Add realistic order/fill lifecycle and partial fills.
- Reconcile cash, positions, fills, and equity.
- Add a kill switch independent of model state.
- Prevent risk limits from being loosened by model or worker identities.

Acceptance criteria:

- Concurrent identical intents create at most one order.
- Concurrent different intents cannot violate aggregate limits.
- Kill-switch activation prevents all new entries immediately.
- Ledger invariants pass property-based and concurrency tests.

Wave 4 integration gate:

- A validated model can enter shadow mode only through policy plus human approval.
- A shadow-qualified model can enter paper mode only through a second recorded approval.
- Paper trades use only trusted current data and hardened risk decisions.

## Wave 5 — Freqtrade Sidecar and Operational Qualification

### WP-17: Freqtrade read-only adapter

Dependencies: WP-06, WP-07 accepted

Builder tasks:

- Deploy Freqtrade as a separately versioned GPLv3 sidecar for crypto only.
- Document license and source-distribution obligations; do not copy Freqtrade internals into this application.
- Implement authenticated API client, timeouts, retries, circuit breaker, and version compatibility check.
- Import crypto instruments, exchange metadata, candles, and backtest results through normalized contracts.
- Keep Freqtrade credentials outside the frontend and control-plane database.

Acceptance criteria:

- Adapter is read-only in this package.
- Sidecar failure cannot degrade equity data or internal paper operation.
- Imported data retains provider, exchange, pair, timeframe, and retrieval provenance.
- Contract fixtures cover API version incompatibility and malformed responses.

### WP-18: Freqtrade dry-run execution adapter

Dependencies: WP-16, WP-17 accepted

Builder tasks:

- Add dry-run order submission behind `ExecutionProvider`.
- Verify `dry_run=true` on startup and before every execution session.
- Translate approved order intents into Freqtrade pair, side, size, and client correlation IDs.
- Reconcile Freqtrade trades/fills into the canonical ledger.
- Treat mode disagreement, timeout, or ambiguous submission as blocked pending reconciliation.
- Do not implement live mode in this package.

Acceptance criteria:

- Adapter rejects a non-dry-run Freqtrade instance.
- Retrying an ambiguous request cannot duplicate exposure.
- Internal and Freqtrade position states reconcile or block further entries.
- Risk approval cannot be bypassed through the adapter.

### WP-19: Qualification monitor and operational scorecard

Dependencies: WP-14, WP-15, WP-16 accepted

Builder tasks:

- Track minimum duration, completed trades, net expectancy, profit factor, drawdown, calibration, regime coverage, slippage, fill quality, prediction gaps, and incidents.
- Define exclusion rules for backfills, corrupted data, manual test orders, and incomplete trades.
- Require a continuous qualification period; critical incidents reset or extend it.
- Generate `live_pilot_candidate` recommendations without performing promotion.

Suggested initial paper qualification:

- At least 100 completed eligible paper trades.
- At least 8–12 weeks of forward operation.
- Positive net expectancy after all modeled costs.
- Profit factor at least 1.20.
- Drawdown below the configured policy maximum.
- Multiple observed market regimes where realistically available.
- No unresolved critical data, security, reconciliation, or scheduler incident.

Acceptance criteria:

- Eligibility can be reconstructed from stored observations.
- Manual or synthetic trades cannot count.
- Recent winning streaks cannot override sample, duration, drawdown, or incident gates.

Wave 5 integration gate:

- Crypto paper execution works end-to-end through Freqtrade dry-run.
- Internal paper execution remains available for supported instruments.
- Qualification status is deterministic, reproducible, and incapable of creating live orders.

## Wave 6 — Live Broker Boundary and Safety Certification

### WP-20: Live broker sandbox adapter

Dependencies: Waves 0–5 fully accepted

Builder tasks:

- Select exactly one initial broker/venue and sandbox environment.
- Implement account identity, balances, market metadata, order submission, cancellation, status, fills, and position reconciliation.
- Use restricted credentials and secret manager integration.
- Add idempotency, bounded retries, clock checks, rate limits, and ambiguous-state recovery.
- Bind the adapter to an immutable allowed account, operating mode, instruments, and capital ceiling.
- Do not connect production-money credentials during development.

Acceptance criteria:

- Sandbox end-to-end tests cover accepted, rejected, canceled, partial, filled, timed-out, duplicated, and externally modified orders.
- Unknown broker state prevents new entries.
- Adapter refuses unauthorized accounts/instruments/modes.
- Secrets are absent from database, source, logs, traces, and UI.

### WP-21: Independent live-safety controls

Dependencies: WP-20 accepted

Builder tasks:

- Implement an execution gateway separate from model workers.
- Require a short-lived, signed authorization for each live order intent.
- Add hard capital, position, symbol, daily-loss, weekly-loss, and total-drawdown ceilings.
- Add independent kill switch, heartbeat/dead-man behavior, stale-model check, stale-data check, and model-policy binding.
- Require two-step human workflow for first live activation: propose, then approve.
- Require a reason and stronger role for kill-switch disable or capital increase.
- Add alerting for every live transition and anomaly.

Acceptance criteria:

- Direct calls from model/training workers cannot reach the broker.
- Expired, replayed, modified, or improperly signed authorizations fail.
- Loss/data/heartbeat simulations trigger the documented fail-closed behavior.
- Security and safety reviewers independently approve the complete boundary.

Wave 6 safety certification:

- Threat model and failure-mode analysis completed.
- Disaster recovery and credential revocation drills completed.
- Sandbox chaos tests completed.
- No P0/P1/P2 security, money-movement, reconciliation, or risk finding remains.
- Human owner explicitly approves moving to the live-pilot preparation wave.

## Wave 7 — Small Live Pilot and Controlled Scaling

### WP-22: Live pilot activation

Dependencies: WP-19 qualification passed; Wave 6 certified

Builder tasks:

- Add a manual activation workflow for one model version, one policy version, one account, and an explicit instrument allowlist.
- Set a hard pilot capital ceiling that code cannot exceed.
- Start long-only, without leverage or derivatives.
- Use conservative per-trade risk, initially around 0.1–0.25% of pilot capital, subject to owner and professional review.
- Require continuous reconciliation and immediate alerting.
- Automatically pause on loss limits, model/data drift, readiness failure, broker disagreement, or operational incidents.

Acceptance criteria:

- Activation cannot occur without the exact qualified model and recorded human approval.
- Capital ceiling is enforced at the broker gateway, not only in UI or strategy code.
- Pilot can be stopped independently of the application frontend.
- Rollback to paper mode is tested before activation.

### WP-23: Live evidence and tiered scaling

Dependencies: WP-22 accepted and pilot observation period completed

Builder tasks:

- Compare live versus paper expectancy, slippage, latency, fill rate, rejection rate, calibration, drawdown, and regime-specific behavior.
- Define minimum live trade count and observation duration before any scale proposal.
- Implement fixed capital tiers; do not compound or scale continuously.
- Require human approval for every tier.
- Automatically step down or pause when live evidence violates policy.
- Preserve full history so later retraining cannot rewrite pilot evidence.

Acceptance criteria:

- Model cannot increase its allocation.
- Scaling proposals include expected and worst-case exposure.
- Every tier has a maximum duration/loss and rollback rule.
- Deterioration reliably reduces exposure or pauses execution.

## 9. Cross-Cutting Review Checklists

### Data review

- Are timestamps timezone-aware and ordered?
- Are source, venue, timeframe, and retrieval time preserved?
- Can synthetic, stale, or revised data enter qualification?
- Are gaps and duplicates explicit?
- Is training/live feature parity proven?

### Modeling review

- Are splits chronological?
- Is the final holdout protected?
- Are overlapping labels purged or embargoed?
- Is preprocessing fitted only on training data?
- Are costs and benchmarks included?
- Are sample size, uncertainty, calibration, and regimes reported?
- Can repeated tuning overfit the holdout?

### Execution review

- Can a request create duplicate orders?
- Does unknown state fail closed?
- Are decimals and venue precision correct?
- Can models or workers bypass risk approval?
- Do fills reconcile to positions, cash, and equity?
- Are partial fills, cancellations, and external orders handled?

### Security review

- Are all mutations authenticated and authorized?
- Are secrets absent from repository, logs, UI, and database payloads?
- Can roles promote models, disable controls, or raise limits improperly?
- Are audit actor identities trustworthy?
- Are dependencies and container images pinned and scanned?

### Operational review

- Are jobs idempotent and observable?
- Are retries bounded?
- Are timeouts explicit?
- Are alerts actionable and deduplicated?
- Can the system recover after restart or network partition?
- Has rollback been tested rather than only documented?

## 10. Test Matrix Required Before Live Pilot

At minimum, automate these scenarios:

1. Missing, stale, duplicated, reordered, negative, and malformed candles.
2. Synthetic data mislabeled as trusted.
3. Feature leakage and future-fitted normalization.
4. Model artifact corruption or version mismatch.
5. Concurrent duplicate order intents.
6. Broker timeout before and after possible acceptance.
7. Partial fill followed by cancellation.
8. Exchange/broker position changed outside the app.
9. Database restart during order submission.
10. Worker restart during training and prediction.
11. Redis/scheduler outage.
12. Incorrect system clock.
13. Kill switch during an in-flight order.
14. Risk-limit change during concurrent decisions.
15. Authentication bypass and privilege escalation attempts.
16. Credential rotation and emergency revocation.
17. Model drift, calibration deterioration, and regime shift.
18. Daily/weekly/total loss-limit activation.
19. Paper/live operating-mode disagreement.
20. Rollback from live pilot to paper-only operation.

## 11. Definition of Done for the Entire Program

The goal is achieved only when all statements below are true:

- Training and evaluation are reproducible from immutable data and feature versions.
- Leakage-aware walk-forward and untouched holdout evaluation pass the approved policy.
- Shadow and paper qualification pass required sample, duration, performance, calibration, regime, and operational gates.
- The production API, secrets, migrations, and deployment have passed independent review.
- The live broker boundary has passed sandbox, reconciliation, security, and failure-mode tests.
- A human explicitly approves a named model version, policy version, broker account, instrument allowlist, and pilot capital ceiling.
- The initial live pilot uses only money the owner can afford to lose, with no leverage and strict loss limits.
- The kill switch and automatic demotion paths have been tested.
- Live evidence is continuously compared with paper expectations.
- Scaling remains fixed-tier, bounded, reversible, and human-approved.

Passing these controls does not guarantee profit. It establishes evidence, operational discipline, bounded exposure, and rapid shutdown when live behavior differs from research.

## 12. First Execution Assignment

Start four workers in parallel:

- Builder A: WP-00 test harness and CI.
- Builder B: WP-01 ADRs and domain contracts.
- Builder C: read-only audit supporting WP-02, enumerating every endpoint and required role; implementation begins after WP-01 contract acceptance.
- Builder D: read-only database-state audit supporting WP-03, identifying all schema states, tables, constraints, and migration conflicts.

When WP-00 and WP-01 builders finish, assign two different reviewer agents. Builders fix all findings and reviewers re-check. Only after both packages are accepted should Builders C and D implement WP-02 and WP-03 against the accepted contracts and test harness.
