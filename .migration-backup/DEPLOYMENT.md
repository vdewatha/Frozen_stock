# Deployment Runbook

This app is still paper-only by default. Keep `ALLOW_LIVE_TRADING=false` in every hosted environment until the strategy evidence, risk controls, broker integration, and human review process are ready.

Before any startup command below, configure [API authentication](docs/AUTHENTICATION.md)
and follow the [database migration lifecycle](docs/DATABASE_MIGRATIONS.md).
API/worker startup does not create tables. Existing unversioned databases need a
backup and reviewed staging adoption, not a blind upgrade or stamp. For a new
empty database run `alembic upgrade head` from `backend/`, then
`python -m app.db.schema`. Local Compose performs that migration in a dedicated
job and binds published ports to loopback only. Its fixed database credentials
are development defaults, not a production secret-management pattern.

The deployment-monitor CLI requires `AUTH_VIEWER_KEY`; remote monitor origins
require HTTPS. The CLI does not follow redirects or print infrastructure secrets.

## Local Runtime Tools

Installed locally on this Mac:

- Docker Desktop with Docker CLI and Docker Compose
- Redis Open Source built at `$HOME/.local/redis/bin`
- Vercel CLI
- Railway CLI
- Fly.io `flyctl`
- Render CLI

Useful local PATH for this shell:

```bash
export PATH="$HOME/.local/bin:$HOME/.local/node/bin:$HOME/.fly/bin:$HOME/.local/redis/bin:$PATH"
```

## Local 24/7-ish Run

For local always-on collection while the Mac is awake:

```bash
export PATH="$HOME/.local/redis/bin:$PATH"
redis-server --port 6379 --dir /tmp

cd backend
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
.venv/bin/celery -A app.tasks.celery_app worker --loglevel=INFO -Q learning --concurrency=2
.venv/bin/celery -A app.tasks.celery_app worker --loglevel=INFO -Q default,market_data,paper_trading,risk --concurrency=1
.venv/bin/celery -A app.tasks.celery_app beat --loglevel=INFO

cd ../frontend
npm run dev
```

## Docker

Build images:

```bash
docker compose build backend frontend
```

Run the full local stack:

```bash
docker compose up -d
```

The Compose stack includes Postgres, Redis, the backend, frontend, Celery workers, and Celery beat.

Run the deployment monitor against the Compose backend:

```bash
docker compose run --rm deployment-monitor
```

This service is behind the `monitor` profile, so it runs only when called directly.

## Render

This repo includes `render.yaml` as a starting blueprint with:

- Postgres
- Redis
- backend web service
- learning worker
- scheduler worker
- frontend web service

Authenticate before deploying:

```bash
render login
render workspace set
render blueprints validate ./render.yaml
```

Then connect the repository through Render Blueprints or use the Render dashboard to apply the blueprint.

## Other Providers

Installed CLIs require account login before deployment:

```bash
vercel login
railway login
flyctl auth login
render login
```

Because the app needs a backend, frontend, Postgres, Redis, workers, and scheduler, Render or Railway are better fits than Vercel alone. Vercel is suitable for the frontend only unless the backend, Redis, workers, and database are hosted elsewhere.

## Market Data Safety

The backend now tries:

1. `yfinance`
2. Yahoo chart API direct fallback
3. `source="unavailable"` with `rows_imported=0`

It no longer writes generated sample prices through the market import endpoint. Synthetic prices may still be used by isolated demo/backtest paths when no real history exists, but those responses are explicitly marked `synthetic_fallback` and should not be used for live-money decisions.

Run database migrations before importing production data:

```bash
cd backend
alembic upgrade head
```

The `market_prices` table stores `source` and `imported_at` provenance for every row. `/system/readiness` blocks automated paper trading when the latest active-symbol market data is missing or comes from an untrusted source. Current trusted market import sources are `yfinance` and `yahoo_chart`.

After deployment, run a fresh market import and confirm readiness before enabling scheduled paper signals:

```bash
curl -X POST "$API_URL/market-data/import" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"SPY","period":"2y"}'
curl "$API_URL/system/readiness"
```

Do not treat an environment as live-money ready unless readiness shows recent trusted market data for all active assets. Existing migrated rows without provenance are marked `unknown` and intentionally block readiness until a real import refreshes them.

Use the deployment monitor as the hosted go/no-go check:

```bash
curl "$API_URL/system/deployment-monitor"
```

The monitor returns `deployable: true` only when the database is reachable, Redis is reachable, required scheduled jobs are configured, live trading remains disabled, and readiness confirms recent trusted market data. Treat any `blockers` entry as a hard stop before 24/7 paper operation.

The app also schedules `deployment-monitor-job` every 15 minutes through Celery beat. That internal job writes a `deployment_monitor` audit log row on every run, keeps one critical `deployment_monitor` notification open while deployment is blocked, updates that notification instead of duplicating it, and resolves it after the deployment monitor becomes ready.

Trigger the internal monitor manually:

```bash
curl -X POST "$API_URL/system/deployment-monitor/run"
```

For cron, hosted scheduled jobs, or a simple local monitor, use the dependency-free script:

```bash
python3 scripts/check_deployment_monitor.py \
  --api-url "$API_URL" \
  --jsonl logs/deployment-monitor.jsonl
```

The script exits `0` only when `deployable` is true. It exits nonzero when the endpoint is unreachable, JSON is invalid, Redis is down, trusted market data is missing, scheduled jobs are misconfigured, live trading is unlocked, or readiness is blocked. Use `--allow-blocked` only for dry-run logging where you do not want the scheduler to alert.

Hosted scheduler examples:

```bash
# Render Cron Job or worker command, with API_URL set to the backend URL
python scripts/check_deployment_monitor.py --api-url "$API_URL"

# Railway cron, Fly machine cron, or Oracle VM cron
*/15 * * * * cd /path/to/Trading\ App && python3 scripts/check_deployment_monitor.py --api-url "$API_URL" --jsonl logs/deployment-monitor.jsonl
```

For container-based hosts that use the backend image, run the backend-local script path:

```bash
python scripts/check_deployment_monitor.py --api-url "$API_URL"
```

For repository-root jobs, run the root wrapper:

```bash
python3 scripts/check_deployment_monitor.py --api-url "$API_URL"
```
