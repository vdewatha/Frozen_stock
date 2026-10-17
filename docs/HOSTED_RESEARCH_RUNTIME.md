# Hosted research runtime

The public web deployment is intentionally an API/UI deployment, not a durable
worker. Replit Autoscale can scale an idle service to zero, so a daemon thread
inside the API is only a best-effort warm-instance collector. Its Redis lease
prevents duplicate polls when several API instances are alive, but it cannot
keep an instance alive.

## Safe deployment shape

Use the existing Autoscale deployment for the read-only UI and API, and run
`artifacts/api-server/backend/scripts/run_hosted_iex_research.py` as a separate
Replit Reserved VM background worker. Configure the worker with the same
production database and Redis URL, the read-only Alpaca data credentials, and:

```text
ENVIRONMENT=production
PAPER_HOSTED_RESEARCH_WORKER=true
ALLOW_LIVE_TRADING=false
IEX_RESEARCH_ENABLED=true
ACTIVE_MARKET_DATA_PROVIDER=alpaca_iex
ACTIVE_PAPER_BROKER=alpaca_paper
```

The worker acquires the existing Redis lease before every poll, stores only
authenticated Alpaca IEX observations, advances online research after a real
observation, and never imports an order task. If a persistent worker is not
available, a Scheduled deployment can invoke the same command once per minute
during market hours; each invocation is idempotent and lease-coordinated.

This does not authorize paper or live execution. It only makes multi-symbol
research collection durable enough to measure. Execution remains separately
gated by the existing paper controls, risk checks, and operator authorization.
