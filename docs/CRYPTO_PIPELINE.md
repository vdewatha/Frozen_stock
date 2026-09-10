# Persistent observation pipeline

The pipeline continuously accumulates Kraken BTC/USD hourly candles and runs registered, version-bound shadow models. It **never submits paper or live orders**, promotes models, or grants qualification.

1. Explicitly select a backed-up, migrated database with `DATABASE_URL` using the existing migration guide. This runner only checks the schema; it does not migrate or seed data.
2. Run `python backend/scripts/run_crypto_pipeline.py` for one cycle. A blocked/partial/error cycle exits nonzero and emits sanitized JSON.
3. Register a research bundle and bind its portable logistic JSON through the shadow API. Without bindings the runner only collects data.
4. Run `python backend/scripts/run_crypto_pipeline.py --continuous --interval-seconds 60` under a supervised process. SIGINT/SIGTERM interrupts the wait and finishes the bounded in-flight provider request before exit. Each public request has a 10-second default timeout. Check JSON output and persisted collection/shadow audit rows; this is not an alert delivery system.

Docker optionally provides `docker-compose.crypto.yml`: use `docker compose -f docker-compose.yml -f docker-compose.crypto.yml --profile crypto up crypto-pipeline`. The override depends on the ordinary migration service and uses the existing Postgres volume. Do not enable duplicate schedulers unnecessarily. Inspect failures before restarting or repairing history.

The base Compose file validates its `AUTH_*` variables during parsing even when only this profile is selected. Supply the four unique API secrets through the environment or the usual protected `.env` file as described in the authentication guide; the crypto runner itself does not receive those secrets.

Collection commits first. Each model then runs in a separate transaction, so one invalid model cannot roll back trusted observations or other models. Outcome scoring uses another transaction. Duplicate hourly predictions reuse the immutable existing decision. `observed_count` is successful binding evaluations, including already-recorded decisions, not a count of newly eligible trades. Every observation and outcome remains research-only.

The first collection retrieves only Kraken's recent rolling window. Older historical data require a separate vetted source. Polling does not manufacture the months of forward evidence needed for qualification. No external credentials are required, and no process is automatically started by installing these files.
