# Probabilistic Paper Trading Research App

This repository contains the first scaffold for a paper-only trading research platform. It is built to test, rank, and improve strategies under deterministic risk controls. Version 1 does not live-trade.

Current implementation status, accepted review increments and remaining gates:
[Build progress](docs/BUILD_PROGRESS.md). The historical phase notes below are
not certification of readiness. No model has qualified for real-money trading.

## Stack

For the first implemented research integrations, test commands, upstream revisions,
independent review record, and additional repository recommendations, see
[Research integrations](docs/INTEGRATIONS.md). The longer delivery roadmap is in
[Multi-agent build plan](MULTI_AGENT_BUILD_PLAN.md).

- Backend: FastAPI, SQLAlchemy, Alembic, Celery
- Frontend: Next.js, Tailwind CSS, Recharts
- Infrastructure: PostgreSQL, Redis, Docker Compose
- Local default database: SQLite for quick development

## Quick Start

First configure four distinct API role secrets of at least 32 characters as
described in [Authentication](docs/AUTHENTICATION.md). Docker Compose requires
`AUTH_VIEWER_KEY`, `AUTH_RESEARCHER_KEY`, `AUTH_OPERATOR_KEY` and `AUTH_ADMIN_KEY`.
Keep them in local secret configuration, not source control. An existing database
requires the [backup/staging migration procedure](docs/DATABASE_MIGRATIONS.md).

```bash
docker compose up --build
```

Then open:

- Frontend: http://localhost:3000
- Backend health: http://localhost:8000/health
- API docs are disabled; authenticated clients use documented endpoints.

Sign in to the frontend with the appropriate role key. Compose runs a dedicated
migration job before starting the API; workers verify the schema before starting.

For local backend-only development:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
python -m app.db.schema
uvicorn app.main:app --reload
```

The migration command above is for a new empty database. Do not run it blindly
against an existing unversioned database. Configure role secrets for the local
backend too. Offline model training does not require starting the API:
[Training and registry instructions](docs/RESEARCH_TRAINING.md).

For local frontend-only development:

```bash
cd frontend
npm ci
npm run dev
```

## Safety Contract

- The app is paper-trading only.
- AI is limited to explanations, summaries, and experiment suggestions.
- Every trade decision must pass the deterministic risk engine.
- Strategies are paused or retired when they violate performance/risk rules.
- Walk-forward validation is required before a strategy can be promoted.

## Current Phase

Phase 1 is implemented with the core project structure, database models, Alembic migration, FastAPI endpoints, strategy/risk/backtest services, Celery, Docker Compose, and a dashboard shell.

Phase 2 has started:

- `/market-data/import` imports OHLCV data through `yfinance`, then Yahoo's chart API if `yfinance` fails.
- If real providers are unavailable, the importer returns `source: unavailable` with zero imported rows instead of storing synthetic prices as current market data.
- `/market-data/{symbol}` returns persisted price history.
- Signals and backtests use persisted prices when available.
- The frontend Market Data & Backtest Lab can import a symbol, load history, and run a strategy backtest.

Phase 3 has started:

- `/models/predict` generates technical features, forward-return targets, probability forecasts, and walk-forward validation folds.
- The model layer uses chronological train/test folds instead of random splits.
- Logistic regression and random forest probabilities are blended for 1-day, 5-day, and 20-day horizons.
- The frontend Probabilistic Model Lab shows probability-up, expected return, validation quality, and latest feature snapshots.

Recommended next phase: persist model predictions and validation runs in database tables, then feed paper-trading outcomes into `strategy_memory`.

Phase 4 has started:

- `/paper-trading/run-signal` generates a strategy signal, stores it, checks deterministic risk rules, and opens an internal paper trade when approved.
- `/paper-trading/reconcile` checks open paper trades against stop-loss, take-profit, and max-holding-period exit rules.
- `/paper-trading/close/{trade_id}` manually closes a paper trade from the control room.
- Closed paper trades update `strategy_memory`.
- The frontend Paper Trading Simulator can run signals, reconcile trades, close open trades, and inspect strategy memory.

Recommended next phase: persist model prediction runs, add strategy promotion/demotion automation, and add audit-log tables for every risk decision.

Phase 5 has started:

- `audit_logs` records risk decisions, paper-trade events, manual closes, and strategy governance decisions.
- `/audit-logs` exposes the latest audit trail.
- `/strategies/evaluate` applies lifecycle rules to promote, pause, or hold strategies from paper-trading memory.
- `strategy_promotion_job` now runs the governance evaluator.
- The frontend Strategy Governance panel can run evaluations and inspect recent audit events.

Recommended next phase: persist model prediction runs and create a separate model-performance table that compares predicted probabilities with realized forward returns.

Phase 6 has started:

- `model_predictions` stores each saved horizon forecast with features and model blend probabilities.
- `model_validation_folds` stores walk-forward validation folds from each persisted model run.
- `/models/run` persists the current model run.
- `/models/score-realized` scores stored predictions once enough future price history exists.
- `/models/performance` summarizes stored prediction count, realized count, hit rate, Brier score, and recent predictions.
- `daily_feature_generation` now persists model runs for active assets.
- `risk_monitor_job` now scores realized model predictions.
- The frontend Model Performance Tracker can save runs, score realized outcomes, and inspect stored prediction rows.

Phase 7 has started:

- Backtests now use the stored strategy parameters, so parameter changes affect signal generation and historical tests.
- `/experiments/run` compares current strategy parameters with proposed candidates and stores each result in `strategy_experiments`.
- `/experiments` returns persisted experiment rows with old/new parameters, score deltas, metrics, decisions, and audit-backed reasons.
- Candidate promotion is gated by rejection rules, trade count, drawdown, and minimum score improvement.
- `nightly_backtest_job` now runs controlled parameter comparisons for active assets without automatically applying changes.
- The frontend Experiment Manager can run comparisons, inspect persisted results, and optionally apply the best qualifying promoted parameter set.

Phase 8 has started:

- `/market-regimes/detect` classifies the current market regime from SPY trend, realized volatility, and recent return proxy features.
- `/market-regimes` and `/market-regimes/latest` expose persisted regime history.
- `daily_market_regime_detection` now stores the latest regime and writes an audit event.
- Paper-trading signals store the current regime and regime feature snapshot.
- Strategy memory is now segmented by market regime, so governance can evaluate strategy behavior in the environment where paper trades occurred.
- The frontend Market Regime Monitor can detect, refresh, and inspect regime history.

Phase 9 has started:

- `/news/import` stores mocked fallback headlines for a symbol with deterministic sentiment and relevance scores.
- `/news` and `/news/{symbol}/summary` expose stored headlines and aggregate sentiment context.
- `daily_news_import` now refreshes fallback headlines for active assets.
- Strategy signal explanations and paper-trade signal records include news context while the deterministic risk engine remains the only trade approval gate.
- The frontend News Sentiment Context panel can import, refresh, inspect headline sentiment, and shows the explicit boundary that news informs explanations only.

Phase 10 has started:

- `economic_indicators` stores fallback macro observations for rates, inflation, unemployment, and a VIX-like proxy.
- `/economic/import`, `/economic/indicators`, and `/economic/context` expose macro ingestion, observations, and summarized macro regimes.
- `daily_economic_data_import` is scheduled alongside market data, news, feature generation, regime detection, paper trading, and governance jobs.
- Market-regime detection now includes macro context and can classify policy-stress conditions.
- Model prediction responses and persisted model features include macro context for review.
- The frontend Economic Context panel can import, refresh, inspect latest macro observations, and shows that macro context never approves trades.

Phase 11 has started:

- A broker adapter service now exposes an `alpaca_paper_stub` shaped paper-order path.
- `/broker/status` reports paper routing as enabled and live trading as blocked.
- `/broker/paper/orders` accepts paper-only test orders and writes broker-order audit events.
- `/broker/live/orders` always blocks live-order attempts in Version 1 and writes an audit event.
- Paper-trading signal fills now route through the paper broker stub after deterministic risk approval.
- The frontend Broker Safety Adapter panel can test paper routing and verify the live-order guard.

Phase 12 has started:

- `/safety/kill-switch/enable` and `/safety/kill-switch/disable` update the active risk rule and audit the action.
- `/safety/strategies/pause` pauses all strategies and records affected strategy IDs.
- `/safety/strategies/resume` moves paused strategies back to paper-trading candidate status for review.
- Dashboard risk state now reports the current kill-switch state.
- The header safety controls are wired to real backend actions with user-visible status feedback.

Phase 13 has started:

- `/risk/settings` returns the active paper-trading risk rule.
- `PATCH /risk/settings` validates editable limits, forces `paper_only`, preserves kill-switch state, and writes an audit event with old/new values.
- The dashboard Risk Settings panel now edits minimum confidence, drawdown caps, position caps, symbol exposure, risk per trade, and consecutive-loss pause limits.
- Invalid values are rejected by API validation before they can reach the persisted risk rule.

Phase 14 has started:

- `/audit-logs` can now filter by event type, action, and entity type.
- The dashboard Audit History panel can switch between all events, risk-setting changes, safety controls, and strategy lifecycle decisions.
- Risk-rule audit rows show compact before/after limit changes while preserving the full JSON payload in the stored audit log.
- Strategy lifecycle audit rows show status transitions and the paper-trading evidence used by governance.

Phase 15 has started:

- `/portfolio/risk` summarizes the current paper ledger with gross exposure, open positions, unrealized P/L, symbol exposure, strategy concentration, data sources, and risk alerts.
- The deterministic risk engine now enforces `max_open_positions_per_strategy` during paper-trade approval.
- Risk-decision audit payloads include both symbol exposure and strategy open-position counts.
- The dashboard Portfolio Risk Monitor shows exposure utilization, limit warnings, breach alerts, and the open-position ledger.

Phase 16 has started:

- `POST /portfolio/risk/actions` evaluates current portfolio breaches against the previous risk-action audit event.
- First-time breaches are observed and audit logged; repeated breaches can automatically enable the kill switch or pause strategies.
- The scheduled `risk_monitor_job` now runs both model-realization scoring and portfolio breach action evaluation.
- The Portfolio Risk Monitor can dry-run or execute automated safety actions, then shows breach count, persistent breach count, and action count.

Phase 17 has started:

- `/strategies/reactivation-queue` lists paused and paper-trading candidate strategies awaiting review.
- `/strategies/reactivation-review` records approve, reject, and hold decisions with eligibility blockers and memory evidence.
- Approval back to active paper trading is blocked when the kill switch is enabled or paper evidence is below reactivation thresholds.
- The dashboard Reactivation Review panel shows blockers, paper evidence, and explicit review actions before a strategy can return to active paper trading.

Phase 18 has started:

- `notifications` persists operational notices with category, severity, status, source, entity context, payload, and timestamps.
- Risk-alert actions and reactivation reviews now create notifications alongside audit events.
- Scheduled jobs now create critical notifications before re-raising failures.
- `/notifications` lists filtered notifications, and `/notifications/{id}/acknowledge` plus `/notifications/{id}/resolve` manage notification status.
- The dashboard Notification Center shows open/acknowledged/resolved notifications with acknowledge and resolve controls.

Phase 19 has started:

- `/system/readiness` evaluates market data recency, stored model freshness, broker paper/live safety, kill-switch and portfolio risk state, strategy availability, unresolved critical notifications, and scheduler failure notifications.
- The readiness response returns overall status, per-check statuses, check details, and whether automated paper runs are currently allowed.
- The dashboard System Readiness panel shows ready, warning, and blocked counts before the research labs.

Phase 20 has started:

- Manual and scheduled paper-trading signal runs now share a readiness preflight gate inside `run_paper_signal`.
- When readiness has blocked checks, the run returns an approved-false response before creating a strategy signal, paper trade, or broker-shaped paper order.
- Blocked preflight attempts are audit logged as `readiness_gate` events with the full readiness snapshot for review.

Phase 21 has started:

- The dashboard Audit History panel now includes a Readiness view filtered to `readiness_gate` audit events.
- Readiness-gate rows summarize the attempted symbol, strategy, overall readiness state, paper-run gate state, blocked checks, and warning checks.
- The loaded audit mix now counts readiness-gate events alongside risk, safety, and lifecycle activity.

Phase 22 has started:

- The System Readiness panel now derives stale and missing active assets from the Market data check details.
- A Refresh Data action imports market data for those affected symbols and then refreshes the readiness snapshot.
- The action is disabled when no active assets need market-data repair.

Phase 23 has started:

- `/trade-candidates` scans active assets and strategies for future paper-trade candidates.
- Candidate ranking combines the probabilistic model's positive expected-return horizon, technical strategy agreement, backtest score, news context, macro context, market regime, and data source.
- The dashboard Prediction Scanner shows positive candidates, watch items, expected return, probability up, horizon, backtest score, and blockers.

Phase 24 has started:

- `model_predictive_long` is now a registered long-only strategy that uses the probabilistic model's positive expected-return horizon as its native signal.
- The model-predictive strategy is included in fresh seed data, dashboard strategy selectors, and the experiment manager.
- The local paper system activated `Model Predictive Long` for paper-only validation after scanner evidence and opened a SPY paper trade through the paper broker stub.
- `/system/readiness` is now ready with market data, model freshness, broker safety, risk state, strategy availability, notifications, and scheduler checks all passing.

Recommended next phase: score the open model-predictive paper trade after new market data arrives, then feed the result into strategy memory and governance.

Phase 25 has started:

- `/trade-scorecard` marks paper trades to market and compares live or realized P/L against the model edge captured at entry.
- The dashboard Predictive Trade Scorecard shows open/closed evidence, entry probability, expected return, current return, and whether each model-supported paper trade is on track.
- The strategy library now includes additional internet-researched strategy families: Bollinger mean reversion, channel breakout trend following, and EMA trend pullback.
- These new strategy families are registered, seeded, visible in strategy selectors, and included in candidate scans/backtests.

Phase 26 has started:

- Trade candidate scans now persist `trade_candidate_snapshots` so the dashboard can load cached positive-candidate rankings quickly.
- `GET /trade-candidates` returns cached snapshots by default and supports `refresh=true` to force a full model retrain/rescan.
- The Prediction Scanner now shows cache status and uses a Rescan button for deliberate refreshes.
- Local verification showed a full scanner refresh around 34 seconds and cached retrieval around 3 milliseconds through TestClient, with live cached curl around 20 milliseconds.

Recommended next phase: schedule scanner refreshes after market-data/model updates so candidate rankings stay fresh without making users wait.

Phase 27 has started:

- `trade_candidate_scan_job` is scheduled every six hours to refresh cached model/strategy candidate rankings.
- `daily_feature_generation` now refreshes the candidate snapshot after persisting model predictions.
- The automated paper-trading signal job now selects active `positive_candidate` rows from the cached scanner instead of hardcoding the moving-average strategy.
- The paper signal job skips candidates that already have an open paper trade for the same symbol and strategy, preventing repeated stacking while the risk engine still enforces exposure caps.
- Readiness scheduler health now includes `trade-candidate-scan-job`.

Recommended next phase: add a candidate-to-activation review path so non-active positive candidates like MACD Momentum can be promoted for paper validation when evidence remains positive.

Phase 28 has started:

- `/trade-candidates/activation-review` reviews positive scanner rows and can move a strategy to paper candidate review, activate it for paper trading, or reject it back to research.
- Candidate activation requires a cached positive BUY candidate, preserves kill-switch blocking, stores the scanner evidence in audit logs, and creates an operational notification.
- The Prediction Scanner now shows a Review for Paper action on positive candidates that are not already active.
- Local verification reviewed the SPY/MACD Momentum positive candidate and recorded a `candidate_activation` audit event.

Recommended next phase: add a one-click paper activation decision for candidate-review strategies once risk state and scanner evidence are both favorable.

Phase 29 has started:

- Candidate activation now checks the full system readiness snapshot before moving a scanner-positive strategy into active paper trading.
- Activation is blocked when readiness has blocked checks, including the current SPY exposure-cap breach from the open model-predictive paper trade.
- The Prediction Scanner now shows Activate Paper for positive paper-candidate strategies and surfaces backend block messages in the UI.
- Local verification confirmed SPY/MACD Momentum stayed in `paper_trading_candidate` while activation was blocked by the Risk state readiness check.

Recommended next phase: add a controlled position-exit or resize workflow so exposure can be reduced before activating additional positive candidates.

Phase 30 has started:

- `/paper-trading/reduce/{trade_id}` reduces an open paper position by a requested percentage, submits the matching paper broker order, and records realized reduction evidence in audit logs.
- The Portfolio Risk Monitor Open Position Ledger now shows per-position 50% trim and Close controls so exposure can be reduced without leaving the risk dashboard.
- Cached scanner reads now hydrate strategy status from the live strategy table, preventing stale Activate Paper controls after a strategy has already been activated.
- Local verification trimmed the open SPY Model Predictive Long paper trade from 8% exposure to 4%, clearing readiness and risk state.
- After readiness returned to green, SPY/MACD Momentum was activated from scanner-positive evidence and opened paper trade 8 through the normal risk approval and paper broker path.
- Current SPY paper exposure is split across Model Predictive Long and MACD Momentum at roughly 4% each, and the risk engine again blocks additional SPY entries at the configured symbol cap.

Recommended next phase: add automatic position allocation across multiple positive candidates so freed exposure is distributed by model edge, strategy diversity, and current symbol concentration instead of being consumed by the next manual activation.

Phase 31 has started:

- `/portfolio/allocation-plan` builds a paper-capital allocation plan from positive scanner candidates, model probability, expected return, rank score, backtest score, live strategy status, open paper positions, and symbol exposure caps.
- The allocation planner produces add, activate, trim, hold, wait-for-room, and watch recommendations without changing paper state.
- The Portfolio Risk Monitor now shows an Allocation Plan card above the exposure and ledger views.
- Local verification shows the current SPY Model Predictive Long and SPY/MACD Momentum paper trades both receive Hold recommendations near their score-weighted target allocations.
- The allocator correctly recognizes that SPY has no remaining symbol room while both active positive candidates are already funded.

Recommended next phase: add a paper-only auto-allocation executor that can apply safe add/trim recommendations when readiness is green, then measure whether allocator-weighted trades improve paper P/L versus equal exposure.

Phase 32 has started:

- `/portfolio/allocation-plan/execute` can dry-run or execute paper-only allocation recommendations.
- The executor only adds new paper trades when readiness is green, can apply trim recommendations through the paper broker reducer, and writes `portfolio_allocation` audit events.
- The Portfolio Risk Monitor Allocation Plan now includes Dry Run and Execute controls.
- Local verification showed the current portfolio has no executable allocation changes because both active positive SPY strategies are already near target and SPY is at its symbol cap.

Phase 33 has started:

- `/opportunity-radar` compares active companies using recent price graph features, stored news sentiment, and the best scanner candidate for each symbol.
- The new Company Opportunity Radar ranks SPY, MSFT, AAPL, and QQQ side by side with mini price charts, trend labels, news tone, best trade signal, and an opportunity score.
- Local verification showed SPY ranked highest as a paper-trade candidate while MSFT, AAPL, and QQQ remained watch items.

Recommended next phase: add broader symbol discovery and real news ingestion so the radar can evaluate more companies than the current active watchlist.

Phase 34 has started:

- News ingestion now supports `auto`, `yfinance`, and deterministic fallback providers through `/news/import`.
- The app attempts to import real company news through yfinance and records provider failures as structured fallback results instead of blocking the research workflow.
- `/opportunity-radar` can refresh news for the active company set before ranking opportunities.
- The Company Opportunity Radar Refresh News action now refreshes news context across SPY, MSFT, AAPL, and QQQ before comparing charts, news tone, and scanner evidence.
- Local verification showed yfinance news returning a JSON decode failure in this environment, with the app safely falling back to deterministic stored news while preserving the error for auditability.

Recommended next phase: add an external news API connector or RSS-based provider so the radar has a more reliable real-news source than yfinance when comparing company opportunities.

Phase 35 has started:

- News ingestion now includes a Nasdaq RSS provider using Nasdaq's per-symbol RSS feed pattern.
- Auto news import now tries yfinance first, then Nasdaq RSS, then deterministic fallback, preserving provider errors in the response when both live providers fail.
- The News Sentiment Context provider selector now includes Auto, YFinance, Nasdaq RSS, and Fallback.
- Local verification showed yfinance returning a JSON decode error and Nasdaq RSS timing out in this sandbox, with auto mode safely falling back while recording both provider failures.

Recommended next phase: add a configurable API-key news provider, such as Finnhub, Polygon, or Alpha Vantage, for more reliable company news coverage when public RSS/provider endpoints are unavailable.

Phase 36 has started:

- `/watchlist/discover` returns a curated large-cap starter universe beyond the initial SPY, QQQ, AAPL, and MSFT set.
- `/watchlist/import` activates selected discovered symbols, imports price history, imports news context, and records a `watchlist_discovery` audit event.
- The Company Opportunity Radar now has an Import Large Caps action and a discovery queue preview.
- Local verification imported NVDA, AMZN, and META with price history and news context, expanding the radar to seven active symbols.
- The expanded radar ranked META above SPY as a paper-trade candidate based on chart trend, news context, and scanner evidence.
- Browser verification should use `http://localhost:3000` for the Next dev server; `127.0.0.1:3000` can block dev resources in this Next.js setup.

Recommended next phase: refresh the trade-candidate scanner after watchlist imports so newly activated companies receive fresh full strategy scans before allocator decisions.

Phase 37 has started:

- `/watchlist/import` now accepts `refresh_candidates` and can force a fresh predictive trade-candidate scan after activating/importing watchlist symbols.
- The watchlist import response includes a compact `candidate_scan` summary with total strategy checks, positive candidates, generation time, and the top scanner rows.
- The Company Opportunity Radar's Import Large Caps action now requests that post-import scanner refresh and reports the refreshed evidence in its status message.
- Local API verification refreshed the seven-symbol active universe into 56 strategy checks and 5 positive candidates, with META ranked first across Model Predictive Long, Moving Average Crossover, and Ensemble Strategy.
- Radar verification confirmed the page then reused the fresh cached scanner snapshot quickly, showing META and SPY as paper-trade candidates.
- Browser verification at `http://localhost:3000` saved `frontend/scanner-refresh-radar-qa.png`.

Recommended next phase: move the import-triggered scanner refresh into a background job with progress/status polling so the UI gets fresh evidence without waiting on the full synchronous scanner pass.

Phase 38 has started:

- Added `scanner_refresh_jobs` to track queued, running, complete, and failed trade-candidate scanner refreshes.
- Added `POST /trade-candidates/refresh-jobs` to queue a refresh in FastAPI background work and `GET /trade-candidates/refresh-jobs/latest` for UI polling.
- The Prediction Scanner Rescan action now queues a background refresh, polls status, and reloads the candidate cache when the job completes.
- The Company Opportunity Radar Import Large Caps action now imports symbols, queues scanner refresh evidence, polls status, and refreshes radar from the completed cache.
- Local API verification showed the queue response returning in about 40 milliseconds while the scanner ran in the background, then completed with 56 strategy checks and 5 positive candidates.
- Candidate cache and radar verification confirmed the completed background job feeds the existing scanner snapshot used by `/trade-candidates`, `/opportunity-radar`, and downstream allocation logic.
- Browser verification at `http://localhost:3000` saved `frontend/scanner-refresh-job-ui-qa.png`.

Recommended next phase: add strategy-source metadata and research notes to each strategy so the predictive system can explain which trading idea, market condition, and validation evidence produced each paper-trade candidate.

Phase 39 has started:

- Added strategy research/provenance metadata for every scanner strategy, including source label, source URL, core idea, ideal market condition, confirmation rules, and risk notes.
- Candidate scanner rows now include `strategy_research` on both fresh scans and cached snapshot hydration, so old cached candidates also gain explainability.
- The Prediction Scanner now shows a compact Strategy Thesis block for each candidate with the source, best-fit market condition, and paper-risk note.
- The Company Opportunity Radar now displays the source label behind each best trade signal so multi-company rankings remain explainable.
- Source anchors include QuantConnect indicator/strategy documentation for moving averages, RSI, Bollinger Bands, breakout concepts, and academic MACD research notes where standalone MACD needs confirmation.
- Live API verification confirmed `/trade-candidates` and `/opportunity-radar` return strategy research metadata for cached candidate rows.
- Browser verification at `http://localhost:3000` saved `frontend/strategy-provenance-ui-qa.png`.

Recommended next phase: add a strategy evidence drilldown that shows the exact model horizon, signal features, backtest rejection/approval details, news tone, and risk-room calculation for the selected paper-trade candidate.

Phase 40 has started:

- Added `GET /trade-candidates/evidence?symbol=...&strategy=...` for candidate-level evidence drilldowns.
- The evidence endpoint starts from the cached scanner row, then recomputes the selected symbol/strategy with current market data, model horizons, signal features, backtest summary, news context, macro context, market regime, readiness, and portfolio risk room.
- The Prediction Scanner now has an Evidence action on each candidate card and a stable evidence panel showing model horizons, signal confidence/reason, backtest metrics, news/macro state, and allocation/risk-room status.
- Live API verification for `META` + `model_predictive_long` returned 1d/5d/20d model horizons, BUY signal confidence, backtest score/trade count, negative news tone, 8% symbol room, add allocation recommendation, and blocked readiness status.
- Browser verification at `http://localhost:3000` clicked the top Evidence button and rendered the full drilldown panel; screenshot saved as `frontend/candidate-evidence-drilldown-qa.png`.

Recommended next phase: add a paper-trade decision journal that stores each approved, rejected, or skipped candidate with the full evidence snapshot so future model learning can compare the decision rationale against realized paper-trade outcomes.

Phase 41 has started:

- Added `candidate_decision_journal` to store candidate decisions as a learning-ready dataset with symbol, strategy, decision, status, reason, full evidence snapshot, optional paper trade link, and realized outcome fields.
- Added `POST /trade-candidates/decision-journal` for manual review/skip/reject/approve journal entries and `GET /trade-candidates/decision-journal` for recent journal rows, including realized outcome syncing for linked paper trades.
- Candidate activation reviews now automatically write a decision journal row with the current evidence drilldown snapshot.
- The Prediction Scanner evidence panel now has Review, Skip, and Reject journal actions plus recent journal rows for the selected candidate.
- Local API verification wrote a manual `META | model_predictive_long` review journal entry with three model horizons in its stored evidence snapshot.
- Local API verification also confirmed a candidate-review activation for `META | moving_average_crossover` automatically wrote journal entry `2`.
- Browser verification at `http://localhost:3000` clicked Evidence, recorded a Review journal entry, and displayed recent journal rows; screenshot saved as `frontend/candidate-decision-journal-qa.png`.

Recommended next phase: use the decision journal as a feedback dataset by adding a journal outcome scorecard that compares approved/skipped/rejected evidence snapshots against later paper-trade or market outcomes.

Phase 42 has started:

- Added a decision journal outcome scorecard that evaluates recorded candidate decisions against linked paper-trade outcomes when available, or market follow-through from stored price history when no paper trade is linked.
- Added `GET /trade-candidates/decision-scorecard` with journal counts, scored counts, hit rate, average outcome return, decision buckets, and row-level quality labels such as pending, good approval, bad approval, avoided loss, and missed gain.
- The scorecard uses the candidate evidence snapshot's model horizon, entry date, expected return, and probability to compare rationale against later outcomes.
- Skip/reject decisions are scored with opposite sign logic so avoiding losses is good and missing gains is bad.
- The Prediction Scanner now shows a Learning Scorecard beside the evidence/journal panel with journaled decisions, scored outcomes, hit rate, average return, and recent row quality.
- Local API verification showed 3 journaled META decisions and correctly marked all 3 as pending because they were created at the latest available price date and no later market data exists yet.
- Browser verification at `http://localhost:3000` showed the Learning Scorecard with 3 journaled, 0 scored, and pending rows; screenshot saved as `frontend/decision-journal-scorecard-qa.png`.

Recommended next phase: run the decision scorecard after each market-data import and paper-trade reconciliation job, then create notifications when previously pending journal decisions become scored.

Phase 43 has started:

- Added `refresh_decision_journal_outcomes` to persist journal outcomes from linked paper trades or market follow-through from stored price history.
- The refresh detects transitions from pending to scored and creates `decision_journal` notifications only when a journal entry receives its first realized/market outcome.
- `/market-data/import` and `/paper-trading/reconcile` now refresh decision journal outcomes after importing prices or reconciling trades.
- `daily_market_data_import` and `paper_trade_reconciliation_job` now run the same journal outcome refresh and return the refresh summary in their job payloads.
- Local reconciliation verification returned `decision_journal: {checked: 4, updated: 1, newly_scored: 1}` after a controlled backdated META journal row became scoreable from existing market prices.
- The scorecard marked that skip decision as `missed_gain` with an 8.45% market follow-through return from 2026-05-01 to 2026-05-29.
- Notification verification created an open `decision_journal` notification titled `Journal decision scored` for journal entry 4.
- Browser DOM verification at `http://localhost:3000` showed the Learning Scorecard updated to Journaled 4, Scored 1, Hit rate 100%, Avg return 8.5%, and the scored row `META | skip | missed_gain | 8.5%`.
- Browser screenshot capture timed out during this phase, so verification is recorded from API responses and browser DOM text.

Recommended next phase: connect decision-journal outcome scores back into strategy memory so strategies that repeatedly produce missed gains, avoided losses, or bad approvals can adjust candidate thresholds over time.

Phase 44 has started:

- Added journal-feedback aggregation into strategy memory through `update_strategy_memory_from_journal`.
- Scored journal outcomes now create `journal_feedback` strategy-memory rows grouped by strategy and symbol, including sample size, average return, win rate, profit factor, confidence score, and guidance notes.
- Feedback distinguishes missed gains, avoided losses, bad approvals, and good approvals so future threshold tuning can tell whether the app was too cautious or too permissive.
- `refresh_decision_journal_outcomes` now updates strategy memory when journal rows transition from pending to scored.
- Added `POST /trade-candidates/decision-scorecard/update-memory` for explicit journal-to-memory refreshes.
- `nightly_strategy_learning_job` now refreshes both closed-paper-trade memory and decision-journal feedback memory.
- The Paper Trading Simulator's Strategy Memory panel now renders memory notes, including journal-feedback guidance.
- Local API verification wrote one `journal_feedback` memory row for `META | model_predictive_long` from the scored missed-gain journal row, with confidence score `0.829050` and guidance to lower review friction for similar setups.
- Frontend production build passed. Browser navigation timed out during this phase, so live UI verification is based on the rendered component code plus `/strategy-memory` API output rather than a browser screenshot.

Recommended next phase: use journal-feedback memory inside the scanner rank score so repeated missed gains, avoided losses, and bad approvals actually nudge candidate rankings and review thresholds.

Phase 45 has started:

- The candidate scanner now separates `base_score` from final `score`, then applies a bounded `journal_feedback` memory adjustment from strategy-memory rows.
- Journal feedback can raise priority after missed gains, lower priority after bad approvals, or preserve cautious treatment after avoided losses while capping the rank-score nudge at +/-0.08.
- Cached scanner snapshots now rehydrate strategy status, strategy thesis, and journal-memory adjustments before sorting, so fresh learning can affect cached candidate order without waiting for a full rescan.
- Candidate API rows now include `memory_score_adjustment`, `review_threshold_adjustment`, and detailed `journal_feedback` notes for auditability.
- The Prediction Scanner UI now shows base score versus adjusted rank score and displays a Journal Memory panel only when scored feedback exists.
- The frontend default API base now uses `http://127.0.0.1:8000`, matching the local backend bind address so client-only scanner calls do not hang on unresolved `localhost` routing.
- Local API verification showed `META | model_predictive_long` moving from `base_score` `0.9620` to adjusted `score` `0.9874` with a `+0.0254` memory adjustment and `-0.0200` review-threshold adjustment from the missed-gain journal row.
- Evidence drilldown verification confirmed the same journal-memory fields are available in the cached candidate evidence payload.
- Browser verification at `http://localhost:3000` showed the Prediction Scanner rendering `JOURNAL MEMORY +0.03`, `Base score 0.96`, `Rank score 0.99`, and `review threshold -0.02` on the `META | Model Predictive Long` row.
- Python compile checks and the frontend production build passed.

Recommended next phase: make the journal-feedback nudge configurable by risk profile and use the review-threshold adjustment inside paper-candidate promotion and allocator decisions, while keeping paper-only safeguards active.

Phase 46 has started:

- Added risk-profile controls for `candidate_review_score_threshold`, `activation_score_threshold`, `journal_feedback_review_threshold_cap`, and `journal_feedback_allocation_multiplier_cap`.
- Risk settings updates now persist those memory-learning controls while continuing to force `paper_only` and preserve the kill-switch state.
- Portfolio risk snapshots now expose the active memory-learning caps so downstream allocation decisions can be audited from the same risk profile.
- Candidate activation reviews now compute a `review_context` with base threshold, bounded journal-memory adjustment, adjusted threshold, candidate score, memory status, and score eligibility.
- Activation verification for `META | model_predictive_long` lowered the activation threshold from `0.72` to `0.70` from journal feedback and passed the score gate, but still blocked activation on the existing `Risk state` readiness blocker.
- Allocation planning now uses journal feedback as a bounded relative sizing multiplier inside each symbol allocation bucket, preserving the symbol exposure cap while nudging stronger learned setups higher.
- Allocation verification showed `META | model_predictive_long` receiving a `1.1035x` memory sizing multiplier, moving target exposure from `2.7569%` base to `2.9375%`, with the recommendation reason noting that journal feedback raised sizing by `10.3%`.
- The Risk Settings UI now shows Review score gate, Activation score gate, Memory review cap, and Memory sizing cap controls.
- The Portfolio Risk Monitor now shows Journal Sizing, the multiplier, base target, and memory status on allocation rows with scored journal feedback.
- Browser verification at `http://localhost:3000` showed `JOURNAL SIZING 1.10x`, `Base 2.8% | raise priority`, and the adjusted `2.9% target` on the `META | Model Predictive Long` allocation row.
- Python compile checks and the frontend production build passed.

Recommended next phase: add a paper-only simulation evaluator that replays past scanner snapshots with and without journal-memory adjustments, so the app can quantify whether the learning loop improves hit rate, expected return, and drawdown before increasing paper allocation.

Phase 47 has started:

- Added `GET /trade-candidates/memory-replay` to compare baseline scanner selection against memory-adjusted selection on stored candidate evidence.
- The replay evaluator uses stored scanner snapshots when future market data exists and also includes decision-journal evidence snapshots so scored review decisions can contribute immediately.
- Baseline selection uses `base_score` and the configured review score gate; memory-adjusted selection uses adjusted scanner `score` plus the bounded review-threshold adjustment from journal feedback.
- Replay output reports evaluated rows, complete rows, pending rows, risk-profile gates, selected counts, hit rate, average return, cumulative return, max drawdown, and deltas between baseline and memory-adjusted policies.
- Recent scanner snapshots now use the latest available market price on or before the snapshot time as the replay entry point, so snapshots newer than the latest imported price are marked pending rather than missing.
- Added a Memory Replay Evaluator dashboard panel with replay coverage, baseline versus memory-adjusted metrics, memory delta, and row-level rank/gate/selection details.
- Local API verification returned 70 evaluated rows, 1 complete row, 69 pending rows, baseline and memory-adjusted selected counts of 42, and equal completed average return of 8.45% while more market data is pending.
- Browser verification at `http://localhost:3000` showed the Memory Replay Evaluator with Rows 70, Complete 1, Pending 69, Baseline and Memory Adjusted hit rate 100%, average return 8.45%, and row-level adjusted gate `0.68` for `META | Model Predictive Long`.
- Python compile checks and the frontend production build passed.

Recommended next phase: add an automatic replay gate that prevents memory-based allocation increases unless the replay evaluator shows enough complete samples and non-negative memory delta versus baseline.

Phase 48 has started:

- Added replay-gate risk controls for `memory_replay_min_complete_samples`, `memory_replay_min_avg_return_delta`, and `memory_replay_min_hit_rate_delta`.
- Risk settings and portfolio risk snapshots now expose those replay-gate controls alongside the existing journal-feedback sizing caps.
- The memory replay evaluator now returns a `replay_gate` object with open/closed status, completed sample count, required sample count, average-return delta, hit-rate delta, cumulative-return delta, and blockers.
- Allocation planning now reads the replay gate before applying memory-based sizing increases. Conservative memory reductions can still apply, but increases are held flat when the replay gate is closed.
- Replay evaluation now caches price history per symbol per run so the replay panel and allocation gate remain responsive.
- Local replay verification showed the gate closed with 1 completed replay sample versus 10 required, while memory and baseline deltas were non-negative but under-sampled.
- Allocation verification showed `META | model_predictive_long` requested a `1.1035x` memory sizing multiplier, but the effective multiplier was held at `1.0`, leaving target exposure equal to the `2.7569%` base target and explaining that the replay gate is closed.
- The Risk Settings UI now includes Replay sample gate, Replay return gate, and Replay hit-rate gate controls.
- The Memory Replay Evaluator UI now shows Replay Gate status, sample coverage, deltas, and blockers.
- Browser verification at `http://localhost:3000` showed `Replay Gate closed`, `1 / 10 complete samples`, and `Only 1 completed replay sample(s); requires 10.`
- Python compile checks and the frontend production build passed.

Recommended next phase: expand the replay evaluator to group performance by symbol, strategy, and market regime so memory-based learning can be approved only for the specific setups where it has proven helpful.

Phase 49 has started:

- The memory replay evaluator now attaches `market_regime` to each replay row.
- Replay output now includes grouped performance summaries under `groups.by_symbol`, `groups.by_strategy`, and `groups.by_regime`.
- Each group includes row count, baseline metrics, memory-adjusted metrics, deltas, and a local replay gate with blockers.
- Group gates use the same risk-profile controls as the global replay gate, making them ready for future setup-specific memory approvals.
- The Memory Replay Evaluator UI now shows compact group cards for By Strategy, By Symbol, and By Regime.
- Local API verification showed grouped summaries for `META`, `SPY`, `Model Predictive Long`, `Moving Average Crossover`, `Ensemble Strategy`, `MACD Momentum`, and `bull_trend`, with the same under-sampled closed gate where applicable.
- Browser verification at `http://localhost:3000` showed `BY STRATEGY`, `BY SYMBOL`, and `BY REGIME` sections, including `Model Predictive Long`, `META`, and `bull_trend` group cards with complete sample counts and closed gate status.
- Python compile checks and the frontend production build passed.

Recommended next phase: use the grouped replay gates inside allocation so memory-based sizing increases can open for a specific symbol/strategy/regime once that exact setup has enough completed evidence, while staying closed elsewhere.

Phase 50 has started:

- Replay output now includes `groups.by_symbol_strategy`, with labels such as `META | Model Predictive Long`.
- Each replay row now carries `symbol_strategy` and `symbol_strategy_label` so setup-level performance can be tracked directly.
- Allocation now chooses the most specific replay gate available for each candidate: symbol+strategy, then strategy, symbol, regime, and finally the global gate.
- Allocation rows now expose `replay_gate_scope` and `replay_gate_scope_label` in `memory_allocation_context`.
- Memory-based allocation increases are now held or released by the chosen setup-specific replay gate instead of only by the global gate.
- Local API verification showed `META | model_predictive_long` using the `symbol_strategy` gate `META | Model Predictive Long`, with 1 completed sample versus 10 required, so the requested `1.1035x` memory sizing multiplier stayed capped at `1.0x`.
- Allocation reason verification now names the setup-specific blocker: `Journal feedback requested higher sizing, but META | Model Predictive Long replay gate is closed.`
- The Memory Replay Evaluator UI now shows a `BY SETUP` section before strategy/symbol/regime groups.
- The Portfolio Risk Monitor now shows the selected gate label on memory sizing rows, for example `Gate: META | Model Predictive Long`.
- Browser verification at `http://localhost:3000` showed `BY SETUP`, the `META | Model Predictive Long` setup card, `JOURNAL SIZING 1.00x`, `requested 1.10x`, and the setup-specific closed-gate reason.
- Python compile checks and the frontend production build passed.

Recommended next phase: add a setup-specific replay approval cache or notification so the system can alert when a symbol/strategy/regime gate opens and memory sizing is allowed to scale for that exact setup.

Phase 51 has started:

- Added replay-gate approval notification sync for memory replay results.
- `GET /trade-candidates/memory-replay` now evaluates open replay gates and creates durable `memory_replay` notifications when global, setup, strategy, symbol, or regime gates allow paper-memory sizing increases.
- Gate notifications are deduped by scope and key, so repeated replay refreshes update the same open notification instead of creating duplicate alerts.
- Gate notifications now resolve automatically when a previously open gate closes under the active paper-memory policy, preventing stale approval notices after risk settings tighten or replay evidence changes.
- Replay responses now include `approval_alerts` with created, updated, resolved, and open-gate counts plus the open gate payloads.
- The Memory Replay Evaluator UI now shows a Gate Alerts panel with open gates, new alerts, refreshed alerts, resolved alerts, and the first approved setup labels.
- Verification temporarily lowered the replay sample gate to 1 completed sample, confirmed the first replay call created 5 gate-open notifications, confirmed the second call updated those same 5 notifications, then restored the sample gate to 10 and confirmed replay resolved all 5 temporary alerts.
- Current restored-policy verification shows the replay gate closed with 1 / 10 completed samples, `approval_alerts.resolved` handling stale approvals, and no open setup approved for memory sizing.
- Browser verification at `http://localhost:3000` showed the Gate Alerts panel with `Open gates 0`, `New alerts 0`, `Refreshed 0`, `Resolved 0`, and `No setup is approved for memory sizing yet.`
- Python compile checks and the frontend production build passed.

Recommended next phase: add a scheduled replay-gate monitor job that runs after market-data imports and paper-trade reconciliation, so setup approvals are discovered automatically without requiring a dashboard refresh.

Phase 52 has started:

- Added `run_memory_replay_gate_monitor` as a reusable service that evaluates replay evidence, syncs gate-open notifications, resolves stale gate approvals, and writes a `memory_replay_gate_monitor` audit log.
- Added `POST /trade-candidates/memory-replay/monitor` so replay-gate notification sync can be triggered directly without relying on a dashboard refresh.
- `/market-data/import` now refreshes decision-journal outcomes and immediately runs the memory replay gate monitor, returning `memory_replay_gate_monitor` in the response.
- `/paper-trading/reconcile` now runs the memory replay gate monitor after reconciliation and journal outcome refresh, so newly closed paper trades can unlock setup-specific memory sizing alerts automatically.
- Added `memory_replay_gate_monitor_job` to scheduled tasks and registered it in Celery beat as `memory-replay-gate-monitor-job` every 30 minutes.
- Scheduler readiness now reports `memory-replay-gate-monitor-job` in the configured jobs list.
- Local monitor verification returned `status: unchanged`, 30 evaluated rows, 1 complete row, 29 pending rows, and a closed replay gate with 1 / 10 completed samples.
- Reconciliation verification returned a nested `memory_replay_gate_monitor` result with 110 evaluated rows, 1 complete row, 109 pending rows, and no gate-open notification changes.
- Direct scheduled-task verification ran `memory_replay_gate_monitor_job.run()` successfully and returned the same closed-gate monitor state.
- Audit verification showed `memory_replay_gate_monitor` rows for both manual API and reconciliation-triggered monitor runs.
- Python compile checks and the frontend production build passed.

Recommended next phase: use opened replay-gate alerts to drive a paper-only allocation review queue, so newly approved setups can be reviewed or dry-run allocated without manual digging through notifications.

Phase 53 has started:

- Added `allocation_review_queue` to turn open replay-gate approval notifications into paper-only allocation review items.
- Added `GET /portfolio/allocation-review-queue`, returning open gate alert counts, dry-run-ready counts, allocation-plan timestamp, and queue rows with gate scope, candidate setup, recommendation, exposure targets, memory multiplier, and paper-only status.
- Allocation recommendations now include `market_regime`, allowing regime-level replay gates to match current allocation rows.
- The queue maps global, symbol+strategy, strategy, symbol, and regime gate approvals to current allocation recommendations.
- Queue rows are deduped by symbol and strategy, choosing the most specific open gate available for each setup instead of showing duplicate global/symbol/strategy/regime rows for the same candidate.
- Queue review status is `ready_for_dry_run` for executor-supported paper actions such as add/trim and `needs_human_review` for activation, hold, wait, or watch recommendations.
- The Portfolio Risk Monitor now includes a Replay Approval Queue panel above the Allocation Plan, showing open gate alerts, dry-run-ready item count, empty state, and the top replay-approved review items when gates open.
- Normal-policy API verification showed the queue empty with 0 open gate alerts and 0 actionable items while the replay gate remained closed at 1 / 10 completed samples.
- Positive-path verification temporarily lowered the replay sample gate to 1, created 5 gate-open alerts, and returned a deduped 5-item allocation review queue with `META | Model Predictive Long` marked `ready_for_dry_run`, `add`, and `1.1035x` memory sizing.
- The replay sample gate was restored to 10, monitor sync resolved all 5 temporary gate alerts, and the allocation review queue returned to empty.
- Browser verification at `http://localhost:3000` showed `Replay Approval Queue`, `0 open gate alerts | 0 dry-run ready`, and `No replay-approved allocation review items are currently open.`
- Python compile checks and the frontend production build passed.

Recommended next phase: add explicit approve/skip actions for allocation review queue items, writing each review decision back into the decision journal so replay-approved setups become part of the learning dataset.

Phase 54 has started:

- Added `POST /portfolio/allocation-review-queue/review` for explicit paper-only approve/skip decisions on replay-approved allocation queue items.
- Added `AllocationReviewRequest` and frontend API support for queue review actions.
- Queue review decisions write to the decision journal with status `allocation_reviewed`, preserving the full evidence drilldown plus an `allocation_review` block that captures the queue item, decision, reason, timestamp, and paper-only flag.
- Reviewed queue items are recorded on notification payloads when available and are also suppressed from future queue output by reading allocation-reviewed decision-journal rows, so refreshed replay notifications cannot re-queue the same symbol/strategy decision.
- The Portfolio Risk Monitor now shows Approve and Skip controls on replay approval queue cards, then refreshes allocation state after a review.
- Positive-path API verification temporarily lowered the replay sample gate to 1, opened 5 replay-gate alerts, approved `META | model_predictive_long` into decision-journal row `7`, skipped `META | moving_average_crossover` into row `8`, and confirmed the reviewed setups were removed from the queue while the remaining queue dropped to 3 items.
- The replay sample gate was restored to 10, the replay monitor resolved all 5 temporary gate alerts, and `/portfolio/allocation-review-queue` returned empty with 0 open gate alerts and 0 actionable items.
- Browser verification at `http://localhost:3000` showed the Replay Approval Queue with Approve and Skip controls while gates were open.
- Python compile checks and the frontend production build passed.

Recommended next phase: add a paper-only dry-run action for an approved allocation queue item so a reviewed setup can simulate the exact add/trim executor path before any paper allocation is applied.

Phase 55 has started:

- Added `POST /portfolio/allocation-review-queue/dry-run` for paper-only single-setup dry runs tied to an approved allocation-review journal entry.
- Added `AllocationReviewDryRunRequest`, requiring symbol and strategy with an optional `journal_entry_id`, so approved queue decisions can be replayed even after the open queue suppresses reviewed items.
- Refactored allocation executor action building into a shared helper used by both whole-plan execution and single approved-review dry runs.
- The approved-review dry run reuses the exact add/trim executor rules, readiness preflight, skip reasons, current allocation plan, and paper-only audit logging.
- The Portfolio Risk Monitor now shows an `Approve + Dry Run` action for queue items whose paper action is executor-supported, approving the queue item into the decision journal and immediately running the single-item dry run.
- API verification used approved journal row `7` for `META | model_predictive_long`; the dry-run endpoint rebuilt the current allocation plan and returned `skipped` because the live readiness state is blocked by `Risk state`, preserving paper-only safeguards instead of staging an add action.
- Restored-policy verification still shows `memory_replay_min_complete_samples` at `10` and `/portfolio/allocation-review-queue` empty with 0 open gate alerts and 0 actionable items.
- Python compile checks and the frontend production build passed.

Recommended next phase: add a queue-reviewed allocation outcome tracker that follows approved dry-run actions over future market data, so the system can learn whether replay-approved allocation decisions would have improved paper P/L.

Phase 56 has started:

- Added a conservative retirement rule to strategy governance so strategies with persistently poor paper evidence can move to `retired` instead of being repeatedly reconsidered.
- Retirement requires at least 10 evidence samples and either a severe drawdown breach or a combined weak win rate, profit factor, and confidence score. Thin evidence can pause or hold a strategy, but it cannot force a positive promotion.
- The governance evaluator still pauses weaker strategies earlier when paper win rate, profit factor, or drawdown fail the existing risk rules.
- The frontend status badge now renders `retired` distinctly from research, paused, and active statuses.
- Governance verification evaluated 8 strategies, held 7, and paused `Moving Average Crossover` because its paper win rate was below the minimum threshold. No strategy was retired because no current strategy met the stricter persistent-failure criteria.
- This preserves the paper-only learning principle: the app records bad outcomes, rejects weak strategies, and avoids treating losing evidence as positive.
- Python compile checks and the frontend production build passed.

Recommended next phase: add a governance scorecard panel that shows why each strategy was held, paused, promoted, or retired, including the exact memory thresholds and recent journal/paper evidence behind the decision.

Phase 57 has started:

- Strategy governance responses now include top-level lifecycle thresholds for promotion, pause, and retirement.
- Each strategy decision now includes rule-level checks for retirement, pause, and promotion, showing actual values, thresholds, comparators, and pass/fail status.
- Governance audit payloads still record the lifecycle action and memory snapshot, while the API response exposes enough evidence for the UI to explain why no positive outcome was forced.
- The Strategy Governance panel now renders a scorecard for each evaluated strategy with decision status, strategy type, memory notes, and Retire/Pause/Promote check groups.
- Decision badges now distinguish hold, pause, promote, retire, paused, and retired states.
- API verification showed `Moving Average Crossover` remaining paused because it has enough pause evidence and weak win-rate/profit-factor checks, while retirement did not trigger because it lacks 10 retirement samples and does not meet all stricter failure criteria.
- Python compile checks and the frontend production build passed.

Recommended next phase: persist the latest governance scorecard snapshot so the dashboard can show lifecycle evidence before the user reruns evaluation, and compare changes between the previous and current governance runs.

Phase 58 has started:

- Governance evaluation now writes an aggregate `strategy_governance_scorecard` audit event with the full lifecycle scorecard payload.
- Added `GET /strategies/governance-scorecard` to retrieve the latest persisted governance scorecard, audit log ID, and creation timestamp.
- The Strategy Governance panel now loads the latest persisted scorecard on page load, so lifecycle evidence is visible before rerunning evaluation.
- The panel shows the persisted snapshot timestamp next to Lifecycle Decisions and updates that timestamp after a fresh evaluation.
- API verification showed the scorecard endpoint returning `empty` before the first aggregate persistence run, then `ready` with audit log ID `199` after `/strategies/evaluate` persisted the scorecard.
- The persisted scorecard keeps the exact threshold checks that explain why strategies are held, paused, promoted, or retired without forcing bad evidence into positive outcomes.
- Python compile checks and the frontend production build passed.

Recommended next phase: add previous-versus-current governance comparison so strategy lifecycle changes, threshold drift, and evidence movement are visible between scorecard runs.

Phase 59 has started:

- The latest governance scorecard API now compares the newest persisted scorecard against the previous aggregate scorecard audit event.
- `GET /strategies/governance-scorecard` now returns previous audit log metadata plus comparison blocks for lifecycle decision/status changes, strategy-memory metric movement, and threshold drift.
- Comparison logic reports new or removed strategies, decision/status changes, sample-size/win-rate/profit-factor/drawdown/confidence deltas, and lifecycle threshold changes.
- The Strategy Governance panel now includes a Previous Run Comparison section with Lifecycle, Memory, and Threshold summaries.
- UI evaluation now refreshes the persisted scorecard after running governance so the displayed comparison uses the backend-created audit snapshot instead of local optimistic state.
- API verification created a new governance scorecard audit row `208`, compared it with previous row `199`, and returned `0 lifecycle change(s), 0 memory movement(s), 0 threshold change(s)` because strategy evidence and thresholds were unchanged between the two runs.
- Python compile checks and the frontend production build passed.

Recommended next phase: add governance notifications when comparison detects a lifecycle change, memory deterioration, or threshold drift, so bad-strategy evidence is surfaced without requiring users to inspect the scorecard manually.

Phase 60 has started:

- Governance evaluation now compares the new aggregate scorecard with the previous scorecard before committing the run.
- Lifecycle comparison changes now open `strategy_governance` notifications when a strategy newly pauses or retires, with retired decisions marked critical and pause decisions marked warning.
- Strategy-memory comparison changes now open warning notifications only when evidence deteriorates, such as lower win rate, lower profit factor, lower confidence, or worse drawdown.
- Governance threshold drift now opens an informational notification with the changed threshold values.
- The `/strategies/evaluate` response now includes the comparison result and a notification summary, so the UI can report how many governance alerts were opened by the run.
- The Strategy Governance panel now updates its status after evaluation with the opened governance notification count.
- API verification evaluated 8 strategies, changed 0 lifecycle statuses, returned `0 lifecycle change(s), 0 memory movement(s), 0 threshold change(s)`, and created 0 governance notifications because the latest evidence matched the previous scorecard.
- Browser verification at `http://localhost:3000` showed the previous-run comparison and confirmed the Evaluate button updated status to `Evaluated 8; changed 0; opened 0 governance notifications`.
- Python compile checks and the frontend production build passed.

Recommended next phase: add notification-center filters or deep links that open the matching strategy governance scorecard row directly from a governance notification.

Phase 61 has started:

- The notification API helper now supports category filters, preserving the existing status filter.
- The Notification Center now includes a category selector for all backend notification categories, including `strategy_governance`, `memory_replay`, candidate activation, reactivation review, risk alerts, decision journal, scanner refresh, and scheduled jobs.
- Governance notifications now show a Governance action that deep-links to the Strategy Governance scorecard panel.
- The Strategy Governance panel now exposes a stable `strategy-governance` anchor target.
- Browser verification at `http://localhost:3000` confirmed the category selector rendered with the `strategy_governance` option, the governance anchor existed, and selecting the governance category refreshed the center to `0 open strategy governance notifications` under the current unchanged-evidence state.
- Python compile checks and the frontend production build passed.

Recommended next phase: add strategy-row targeting to governance notification payloads and UI anchors so a governance alert can jump directly to the affected strategy row, not only to the governance panel.

Phase 62 has started:

- The Strategy Governance scorecard now assigns a stable anchor to every strategy row using `strategy-governance-row-{strategy_id}`.
- Governance row hashes are tracked on the client, and the targeted row is highlighted so a user can immediately see the relevant lifecycle evidence.
- Governance notification actions now deep-link to the affected strategy row when the notification is tied to a strategy entity, while threshold-level governance notifications still fall back to the overall Strategy Governance panel.
- Browser verification at `http://localhost:3000/#strategy-governance-row-1` confirmed the Moving Average Crossover row rendered, highlighted, and preserved its weak-evidence pause explanation instead of forcing any positive status.
- Browser verification also confirmed strategy governance links resolve to row hashes such as `#strategy-governance-row-1`, while non-strategy governance alerts fall back to `#strategy-governance`.
- Python compile checks and the frontend production build passed.

Recommended next phase: create a governed strategy improvement queue that turns paused or retired strategy rows into explicit experiment candidates with required evidence, proposed parameter changes, and paper-only backtest gates before reactivation.

Phase 63 has started:

- Added a read-only `strategy_improvement_queue` service for paused and retired strategies.
- Added `GET /strategies/improvement-queue`, returning queued counts, paused/retired counts, latest memory evidence, improvement reasons, proposed parameter experiments, latest experiment context, and paper-only gates.
- Improvement queue rows preserve weak evidence instead of forcing a positive interpretation. Current verification queues `Moving Average Crossover` as paused with 6 closed paper samples, 0.00 win rate, 0.00 profit factor, and 0.40 confidence.
- Proposed experiments are sourced from the existing learning engine, such as `MA 10/30`, `MA 20/50`, and `MA 50/200`, but the queue does not apply parameters or reactivate strategies.
- Paper gates require backtest improvement, adequate trade count, enough closed paper samples, and human reactivation review before a paused or retired strategy can return to active paper trading.
- Added a Strategy Improvement Queue panel to the dashboard between Governance and Reactivation Review.
- Browser verification at `http://localhost:3000` confirmed the panel rendered 1 weak paused strategy, showed Moving Average Crossover improvement reasons, displayed the MA experiment proposals, and marked the 20-sample paper gate as blocked.
- API verification returned 1 queued strategy, 1 paused, 0 retired, and `paper_only: true`.
- Python compile checks and the frontend production build passed.

Recommended next phase: add a one-click paper-only experiment run from the improvement queue that executes the proposed parameter comparison without applying promotions, then refreshes the queue with the latest experiment decision.

Phase 64 has started:

- Audited the learning loop for previous paper trades, decision-journal outcomes, stored price history, and newer strategy techniques.
- Confirmed closed paper trades update `strategy_memory` through paper-trading reconciliation/manual close paths, and current memory shows `Moving Average Crossover` on SPY with 6 samples, 0.00 win rate, 0.00 profit factor, and 0.40 confidence.
- Confirmed decision-journal market follow-through creates separate feedback memory, including the existing META `journal_feedback` memory bucket with 1 scored outcome and notes about missed gains.
- Confirmed backtests and experiments use stored market price history when available, with synthetic fallback only when history is insufficient.
- Expanded the learning engine with technique-specific parameter proposals for MACD Momentum, Model Predictive Long, Bollinger Mean Reversion, Channel Breakout, and Trend Pullback instead of falling back to a generic risk-filter experiment.
- Updated MACD Momentum and Trend Pullback signal generation so their new parameter proposals actually change the tested signal logic.
- Added a one-click `Run Paper Experiments` action to the Strategy Improvement Queue. It runs the existing parameter comparison with `apply_promotions=false`, refreshes queue state, and does not reactivate strategies or execute trades.
- Verification ran paper-only Moving Average experiments from stored SPY history. All candidates were persisted and rejected by gates such as insufficient historical trade count, low win rate, low profit factor, or low score, preserving weak evidence instead of forcing a positive outcome.
- Browser verification at `http://localhost:3000` confirmed the Strategy Improvement Queue shows the run action, weak SPY/Moving Average evidence, blocked paper sample gate, and latest rejected experiment context.
- Python compile checks and the frontend production build passed.

Recommended next phase: add an experiment coverage scorecard that shows which strategies have recent technique-specific experiments, which rely on stale or generic proposals, and which need more historical price data before experiments are trustworthy.

Phase 65 has started:

- Added dedicated Celery queues for `market_data`, `learning`, `paper_trading`, `risk`, and `default` work so learning can scale without blocking market imports, paper-trade reconciliation, or risk checks.
- Split Docker worker roles into a 4-concurrency learning worker, 2-concurrency market-data worker, 1-concurrency paper/risk worker, a default worker, and Celery beat for scheduled jobs.
- Added a scheduled `strategy_learning_batch_job` every 6 hours. It fans out paper-only scoped jobs by active symbol and strategy, which is close to one worker lane per company+technique without hard-coding one permanent worker per company.
- Added `strategy_learning_scope_job`, which runs strategy experiments for one symbol and one strategy with `apply_promotions=false`; it records experiment decisions but does not execute trades, apply parameters, or reactivate a paused strategy.
- Added `POST /learning/workers/scope`, `POST /learning/workers/batch`, and `GET /learning/workers/{task_id}` so the app can launch and inspect learning jobs through the API.
- Worker launch/status endpoints now return structured `unavailable` JSON when Redis/Celery is offline instead of failing with an opaque server error. Current local verification shows Redis is not running on `localhost:6379`, so queue launch reports unavailable while remaining paper-only.
- Verified a direct scoped SPY Moving Average learning run from stored history. It created experiment `15`, rejected `MA 10/30`, applied no parameters, and returned `paper_only: true`.
- Design note: one worker per chart type or company is useful as a scoped work unit, but it is not the most optimal permanent architecture by itself. The safer pattern is bounded parallel jobs by symbol+strategy, separate queues by workload type, out-of-sample gates, minimum trade counts, drawdown limits, and human review before promotion.
- Safety note: the app should not "learn until positive" as a promotion rule because that can overfit historical noise. Positive paper trades should be treated as evidence only when they persist across walk-forward/out-of-sample tests, enough samples, realistic slippage/fees, and drawdown controls.
- Python compile checks, backend import checks, Celery route/schedule inspection, and the frontend production build passed.
- Docker Compose structure was parsed successfully with Python/YAML, but full `docker compose config` could not run because the Docker CLI is not installed in this environment.

Recommended next phase: add a learning-worker dashboard showing queued/running/completed scopes, experiment coverage by symbol+strategy, stale-history warnings, and guardrail failures before any promotion can be considered.

Phase 66 has started:

- Installed local deployment/runtime tooling where this Mac allowed it: Docker Desktop, Docker CLI, Docker Compose, Redis Open Source 8.8.0, Vercel CLI, Railway CLI, Fly.io `flyctl`, and Render CLI.
- Started Redis locally on port 6379 and verified Celery workers can connect to it.
- Restarted the backend and launched a dedicated learning worker plus a default/market/paper/risk worker.
- Verified `POST /learning/workers/scope` now queues through Redis and completes a paper-only SPY Moving Average learning job. The queued task completed successfully, created experiment `72`, rejected `MA 10/30`, applied no parameters, and returned `paper_only: true`.
- Hardened market-data import reliability. If `yfinance` fails, the backend now tries Yahoo's chart API directly before giving up.
- Removed the dangerous behavior where market import wrote generated sample prices as current market rows. When both real providers fail, import now returns `source: unavailable`, `rows_imported: 0`, and no persisted sample prices.
- Re-imported all active symbols from the real Yahoo chart fallback. AAPL, AMZN, META, MSFT, NVDA, QQQ, and SPY each imported 502 real rows from 2024-06-10 through 2026-06-10 with `source: yahoo_chart`.
- Verified a bogus symbol import returns `source: unavailable`, `rows_imported: 0`, `start_date: null`, and `end_date: null` instead of synthetic data.
- Installed Docker Desktop and verified Docker daemon, Docker CLI, and Docker Compose work locally.
- Converted the frontend Dockerfile to a production standalone Next.js image instead of a dev server image.
- Added Docker ignore files and a backend container default command.
- Added `render.yaml` as a starting Render blueprint for Postgres, Redis, backend, frontend, learning worker, and scheduler worker.
- Added `DEPLOYMENT.md` with local run commands, Docker commands, provider login steps, and market-data safety notes.
- Verified Python compile, frontend production build, Docker Compose config, backend Docker image build, frontend Docker image build, backend image import check, and frontend image standalone artifact check.
- Deployment is not live yet because Vercel, Railway, Fly.io, and Render all require account authentication before creating hosted resources. CLI checks returned unauthenticated states for each provider.

Recommended next phase: authenticate a hosting provider, validate/apply the deployment blueprint there, set production environment variables, run migrations on the hosted database, and add a deployed health/readiness monitor that confirms real market-data imports are not falling back to unavailable or synthetic sources.

Phase 67 has started:

- Added durable market-data provenance to `market_prices` with `source` and `imported_at` columns.
- Added Alembic migration `0002_market_price_provenance` and upgraded the local database to head.
- Market history responses now expose the stored source for each price row and summarize mixed or single-source database histories.
- `/system/readiness` now treats latest active-symbol data as trusted only when it comes from `yfinance` or `yahoo_chart`.
- Existing migrated rows without provenance are marked `unknown`, and readiness intentionally blocks paper trading until a real provider import refreshes them.
- Local verification shows the current database is blocked with AAPL, AMZN, META, MSFT, NVDA, QQQ, and SPY all at `source: unknown`; SPY history reports `database:unknown`.
- A network-restricted import attempt returned unavailable/zero imported rows rather than persisting synthetic prices, preserving the live-money safety gate.
- Python compile checks and Alembic `current` passed at `0002_market_price_provenance`.
- Frontend production build and Docker image rebuild could not be rerun in the current sandbox because process port binding and Docker home writes are denied, but those builds passed before the sandbox tightened.

Recommended next phase: run the app in an unrestricted local shell or hosted environment, execute a fresh real market import for all active assets, confirm `/system/readiness` reports trusted sources, then authenticate a free or low-cost hosting provider for deployment.

Phase 68 has started:

- Added a deployment monitor service and `GET /system/deployment-monitor`.
- The monitor checks database connectivity, Redis connectivity, required Celery beat jobs, trusted market-data readiness, and live-trading lockout.
- `deployable` is true only when infrastructure checks pass and `/system/readiness` is fully ready.
- Local verification returned `deployable: false` with blockers `Redis` and `Trusted market data`, while database, scheduled jobs, and live-trading safety were ready.
- This gives a hosted go/no-go endpoint for Render, Railway, Fly.io, Oracle, or any uptime monitor before allowing 24/7 paper operation.
- Python compile checks passed, and route inspection confirmed `/system/deployment-monitor` is registered.

Recommended next phase: add a small frontend deployment status panel that displays this monitor, highlights blockers, and keeps paper/live-money readiness visible without digging through raw JSON.

Phase 69 has started:

- Added a dashboard Deployment Monitor panel directly below System Readiness.
- The panel calls `GET /system/deployment-monitor`, shows deployable/not deployable status, environment, readiness state, paper-trading gate, and live-trading lockout.
- Deployment blockers and readiness blockers are displayed as first-class UI signals, with detailed checks for database, Redis, trusted market data, scheduled jobs, and live-trading safety.
- Synced the frontend price-history type with backend market provenance by adding `PricePoint.source`.
- TypeScript verification passed with `npm exec tsc -- --noEmit`.
- Frontend production build passed with `npm run build`.
- Backend Python compile checks passed.

Recommended next phase: add a hosted monitor automation or cron-compatible script that calls `/system/deployment-monitor`, records the result, and alerts when Redis, market provenance, scheduler jobs, or live-trading safety regress.

Phase 70 has started:

- Added `scripts/check_deployment_monitor.py`, a dependency-free cron-compatible deployment monitor.
- The script calls `/system/deployment-monitor`, prints a compact status summary, and can append JSONL audit rows for hosted or local monitoring.
- Exit code behavior is fail-closed: `0` only when `deployable` is true, `1` for a reachable but blocked deployment, and `2` when the monitor endpoint is unreachable or invalid.
- Added `--snapshot-file` for offline verification and `--allow-blocked` for dry-run logging without alerting.
- Updated `DEPLOYMENT.md` with script usage and alerting guidance.
- Verification passed for Python bytecode compilation with cache redirected to `/private/tmp`.
- Verification confirmed blocked snapshots exit `1` with Redis/trusted-market-data blockers, ready snapshots exit `0`, unreachable endpoints exit `2`, and `--allow-blocked` exits `0` while still printing blockers.
- JSONL output includes checked time, deployable state, readiness state, blockers, failed checks, and live-trading status.

Recommended next phase: wire this monitor into the deployment target once a provider is authenticated, then add provider-specific scheduled-job examples for the selected free or low-cost host.

Phase 71 has started:

- Added a backend-local `scripts/check_deployment_monitor.py` so the monitor is available inside backend Docker images and hosted backend containers.
- Kept the repository-root `scripts/check_deployment_monitor.py` as a wrapper, preserving the local/operator command.
- Added a `deployment-monitor` one-shot Docker Compose service behind the `monitor` profile.
- `docker compose run --rm deployment-monitor` now checks the backend container through `http://backend:8000` and writes JSONL output inside the one-shot container.
- Updated `DEPLOYMENT.md` with Compose, Render Cron Job, Railway/Fly/Oracle cron, backend-container, and repository-root monitor commands.

Recommended next phase: authenticate the chosen provider, create the scheduled monitor there, and confirm it alerts while the current deployment remains blocked on Redis/trusted market data.

Phase 72 has started:

- Added `run_deployment_monitor`, a side-effecting monitor runner that reuses the deployment monitor snapshot.
- Added `POST /system/deployment-monitor/run` for manual internal monitor runs.
- Added `deployment_monitor_job` to Celery, routed it to the risk queue, and scheduled it every 15 minutes as `deployment-monitor-job`.
- The internal job writes a `deployment_monitor` audit log on every run.
- While blocked, it keeps one critical `deployment_monitor` notification open and updates it instead of duplicating alerts.
- When deployability becomes ready, the same monitor resolves the open deployment notification.
- The deployment monitor now requires its own scheduled `deployment-monitor-job` in the configured beat schedule.
- Verification confirmed direct monitor runs create then update a single critical notification, write two audit rows, and report the current blockers `Redis` and `Trusted market data`.
- Verification confirmed `deployment_monitor_job.run()` returns blocked, updates the existing notification, and preserves the blocker list.

Recommended next phase: surface deployment-monitor notifications in the dashboard notification filters and add a compact audit-history filter for `deployment_monitor` events.
