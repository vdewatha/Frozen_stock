# Offline model training and registration

These commands create experimental research artifacts. They do not connect a
trained model to signals, orders, paper qualification or live trading. Use the
backend's installed Python environment. Commands below run from repository root.

## Train

```sh
python backend/scripts/train_research.py --csv /absolute/path/prices.csv --output /absolute/path/research-runs --symbol SPY --source yahoo-export --horizon 5
```

CSV columns must include `date`, `close`, and `volume`. Dates must be unique,
strictly increasing, and parseable; normalization preserves subsecond precision
in UTC. Prices must be finite and positive, volumes finite and nonnegative.
Supported prediction horizons are 1, 5, or 20 **observations**, not calendar days.
Use one consistent instrument and timeframe per file. Neither the symbol nor the
source text certifies instrument identity or vendor provenance.

The pipeline computes the shared causal features, reserves the final 20% of
eligible observations as the holdout, and removes training labels whose outcome
extends to the holdout boundary. At least 100 training rows, 30 holdout rows,
and two training target classes are required. Raw history must be longer to
account for feature warmup, horizon and purging.

It fits logistic regression and random forest models with a fixed seed, then
records holdout Brier score and log loss alongside a training-prevalence baseline.
No model is selected or promoted from these results. The current legacy feature
scaling remains in use. Classification scores are not cost-adjusted trading
returns, and inspecting the same holdout repeatedly undermines independence.

Output appears atomically in `<output>/<run_id>/`:

- `manifest.json`: hashed input/code/config/environment identity, sample sizes,
  chronology, metrics, limitations and artifact checksums.
- `dataset.csv`: normalized input snapshot.
- `holdout_predictions.csv`: held-out labels and model probabilities.
- `logistic_regression.joblib` and `random_forest.joblib`: fitted models.

Identical input/code/environment identity yields the same run ID; an existing
run directory is never overwritten by training. Joblib can execute Python when
loaded. Never load model files from an untrusted source. The registry below
reads their bytes only and never deserializes them.

## Linux numerical runtime

If the host numerical library emits warnings, do not suppress them. The trainer
rejects numerical/convergence warnings and invalid probabilities before publishing
artifacts. A small isolated Linux image is available:

```sh
docker build -f backend/Dockerfile.research -t trading-research:local backend
docker run --rm --network none \
  --mount type=bind,src=/absolute/path/input,dst=/input,readonly \
  --mount type=bind,src=/absolute/path/research-runs,dst=/output \
  trading-research:local --csv /input/prices.csv --output /output \
  --symbol BTC-USD --source caller_supplied_research --horizon 5
```

Create the output directory first. Use canonical absolute mount paths and quote
the entire mount argument if a path contains spaces. This image has no broker,
database or network access during training. Platform and machine architecture are
recorded in the run identity; models can differ between numerical runtimes. Retain
the image digest for stronger environment reproducibility.

## Register artifacts

First migrate the chosen database according to `DATABASE_MIGRATIONS.md`.
Set `DATABASE_URL` explicitly in the shell environment using the deployment's
secret handling. Then run:

```sh
python backend/scripts/register_research.py --run /absolute/path/research-runs/ACTUAL_RUN_ID
```

The command refuses an unset DATABASE_URL or outdated schema and never migrates
or creates a database schema itself. It verifies required metadata, purged
chronology, finite metrics, exact filenames, checksums and identity. The final
directory name must match the calculated run ID. Parent traversal, symlink
directories/files, unexpected files, duplicate JSON keys and nonregular files
are rejected. Supply a canonical absolute path, including resolving operating
system directory aliases such as macOS `/var` versus `/private/var` beforehand.

Successful registration inserts one `research_model_runs` row with a unique
run ID, canonical manifest digest, artifact location and complete metadata.
Its status is always `experimental`; `eligible_for_trading` is always false,
also enforced by database constraints. Repeating an unchanged registration
returns the existing row. Changing a registered manifest under the same identity
fails instead of replacing it. Concurrent registrations use database uniqueness
and a savepoint; caller rollback leaves no new record. The Python service leaves
transaction commit to its caller; the CLI commits only after successful validation.

Hashes prove internal consistency, not trustworthy data, authorship, ownership or
future profitability. A caller can manufacture a consistent bundle; registration
does not certify it. Local artifact files can still be changed outside this tool,
so consumers must reverify hashes before using them. There is no artifact loading,
registry update/delete, promotion, approval or live execution command here.

Authenticated viewer access is available at `GET /research/runs` (default limit
20, allowed 1–100; nonnegative `offset`) and `GET /research/runs/{run_id}` (64
lowercase hexadecimal characters). The list returns `items`, `total`, `limit`
and `offset`; detail returns one summary or 404. Responses project only run
identity, experimental status, trading ineligibility, sample/date bounds, model
metrics and runtime versions. They omit local artifact paths, files, full
manifests and source claims. Both routes issue read-only database queries; there
are no HTTP training, upload, registration or promotion endpoints.

Tests use temporary SQLite databases and generated research fixtures only.
PostgreSQL 16 migration/schema checks, caller rollback and concurrent registration
also passed in a disposable local container. See `DATABASE_MIGRATIONS.md` for
the reproducible integration script; no production database was accessed.
