# Research integrations: first implementation increment

## Scope and status

The implementation includes a configurable local feature pipeline inspired by Intelligent Trading Bot, corrected chronological evaluation, an offline training pipeline, an integrity-checked experimental model registry, and a read-only Freqtrade adapter. Foundation repairs now include role authentication, migration-driven startup, trusted-data checks, and removal of synthetic research/execution fallbacks. These are scoped increments, not acceptance of all Waves 0–7. Broker execution, trusted crypto ingestion, end-to-end qualification evidence, and live trading remain outstanding.

Existing model results must not be used as evidence for live promotion. Recompute evaluations after the leakage correction. Paper-model realization scoring now uses observed trading bars, not calendar-day offsets. Previously stored rows with unknown provenance remain untrusted; migrations do not certify their origin. Authentication currently uses role-specific service secrets, not individual identity accounts. Qualification is a pure policy evaluator, not an authenticated evidence aggregation service.

For compatibility the initial feature refactor preserves the existing global [-5, 5] clipping, including RSI. That compresses most ordinary RSI values to 5; correcting feature-specific scaling requires a new feature version and retraining. This increment does not certify the existing feature definitions as predictively useful.

## Upstream sources inspected

| Source | Inspected revision | Adopted boundary |
| --- | --- | --- |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | `9f10e357a93c1dcf10c2a2b367659214d89c073e` (stable checkout) | HTTP client for health/configuration/status; external process, no upstream execution code embedded |
| [Intelligent Trading Bot](https://github.com/asavinov/intelligent-trading-bot) | `0f8748d21906d37b7d33de9278f4742b8f419456` | Declarative, reusable feature-generation design; local implementation |
| [quant-trading-bot topic](https://github.com/topics/quant-trading-bot) | Discovery page, no stable code revision | No executable dependency; each repository needs individual evaluation |

Freqtrade is GPLv3; its source and license remain with its separately deployed service. An HTTP boundary is an engineering decision, not a legal conclusion about distribution obligations. Review the exact deployment/distribution before shipping bundled third-party software. Intelligent Trading Bot is MIT; retain its notices for any code copied or adapted. The new local pipeline does not require installing the upstream trading server or its Binance/MetaTrader order modules.

Intelligent Trading Bot currently specifies Python >=3.12 and pandas 3.x, whereas the application's Docker runtime is Python 3.11 with pandas 2.2.2. Its entire dependency graph is therefore not installed into this backend. Freqtrade's strategy signals and ITB composite scores are not calibrated probabilities and must not be mapped directly to `probability_up` without validation.

## Additional useful repositories

Evaluated from official repository pages on 2026-09-04. These are recommendations, not installed integrations. Pin a tested release, inspect its exact license and scan dependencies when its work package begins.

| Repository | Useful feature | Proposed package | Current decision |
| --- | --- | --- | --- |
| [Pandera](https://github.com/unionai-oss/pandera) | DataFrame schemas and data validation | WP-07/WP-08 | Strong candidate for candle/feature quality reports; [MIT license](https://github.com/unionai-oss/pandera/blob/main/LICENSE.txt) |
| [LightGBM](https://github.com/lightgbm-org/LightGBM) | Gradient-boosted tree models | WP-10 | Benchmark after reproducible baseline and validation exist; MIT, native build/runtime compatibility must be tested |
| [MLflow](https://github.com/mlflow/mlflow) | Experiment tracking and model artifacts | WP-09 | Evaluate optional tracking adapter; avoid duplicating the application's model promotion authority |
| [Hypothesis](https://github.com/HypothesisWorks/hypothesis) | Property-based testing | WP-16 and data validation | Useful for cash/position invariants, malformed candles and idempotency inputs |
| [sktime](https://github.com/sktime/sktime) | Time-series baselines and evaluation abstractions | WP-10/WP-11 | Optional later benchmark; do not replace chronological horizon-aware split tests |

Do not add all of these dependencies at once. Each must solve an accepted package's requirement and pass compatibility and independent review. GitHub popularity and a positive example backtest are not qualification evidence.

## Verification environment

### Freqtrade health check

With an existing Freqtrade dry-run instance configured according to its [REST API documentation](https://www.freqtrade.io/en/stable/rest-api/), set `FREQTRADE_URL`, `FREQTRADE_USERNAME` and `FREQTRADE_PASSWORD` through your local secret environment. `FREQTRADE_URL` is the server origin (for example `http://127.0.0.1:8080`), not a URL ending in `/api/v1`. Remote servers require HTTPS. `FREQTRADE_TIMEOUT_SECONDS` defaults to 5 and may not exceed 30.

Run `python backend/scripts/check_freqtrade.py` from the repository root. Success returns a JSON health/configuration summary and open dry-run trade count. Missing credentials, transport failures, invalid responses and a missing/false `dry_run` flag produce a nonzero exit. This command only reads `ping`, `show_config` and `status`; it neither deploys a Freqtrade instance nor imports its ledger into the application.

### Isolated regression tests

From the repository root, use a fresh Python 3.11 or 3.12 virtual environment:

```sh
python3.12 -m venv .venv-research
.venv-research/bin/python -m pip install -r backend/requirements.txt
cd backend
../.venv-research/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

Tests use temporary databases and fixtures, not the existing trading database or broker accounts. The GitHub Actions workflow configures Python 3.11/3.12 backend tests and frontend checks; local success does not imply that hosted CI has run. Migration tests currently exercise SQLite; PostgreSQL deployment still requires a staging verification.

## Acceptance and next work

Each builder submits its changed files and tests to a different reviewer. Reviewers request fixes and independently re-run tests before accepting a scoped increment. Acceptance does not certify profitability or production readiness.

Next foundation work includes individual identities/durable audit retention, canonical instruments/calendars, and per-model readiness. Next research work includes calibration, locked evaluation protocol and trustworthy observed-paper evidence aggregation. Freqtrade now has a tested disposable stopped dry-run startup and Kraken public candle adapter; see [selected paper venue](PAPER_VENUE.md). Next work is durable observation/ledger integration and model-bound paper execution. No live exchange credentials are needed for these increments.

## Review record — 2026-09-04

- Feature builder: configurable causal registry, feature configuration hashing, predictor wiring, nullable unknown labels, timestamp-based purging of overlapping training labels.
- Freqtrade builder: authenticated read-only client and environment-configured CLI with mocked API contract tests.
- Independent reviewer: approved both scoped packages and the documentation/test workflow. Requested a test correction: after missing-row filtering, horizon-1 folds can legitimately purge zero rows; require strict train-label-end < test-start for every fold rather than a positive purge count for all folds. Builder fixed the assertion and reviewer re-ran the suite.
- Integrator: all 18 tests passed using Python 3.12 and pinned numpy 2.0.1, pandas 2.2.2, scikit-learn 1.5.1 and httpx 0.27.2.
- Independent reviewer also ran all 18 tests successfully in its available environment. Hosted Python 3.11/3.12 CI is configured, not yet observed running.
- Acceptance excludes a deployed Freqtrade handshake, full backend/frontend verification, foundation repairs, model profitability, and live trading. The HTTP integration has only been exercised with mocked transport.
