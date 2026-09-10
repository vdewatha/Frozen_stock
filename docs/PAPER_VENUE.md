# Selected paper venue: Kraken spot through Freqtrade

The initial venue is **Kraken BTC/USD spot**, using **Freqtrade dry-run**, not an
exchange-funded account. Start with hourly candles, no leverage or shorting.
This matches a [directly supported Freqtrade exchange](https://www.freqtrade.io/en/stable/exchanges/)
and the application's existing read-only Freqtrade integration.

This selection does not choose or approve a future live account. Exchange
availability, user jurisdiction, account permissions, fees and supported order
types must be verified before any live implementation. No account was created,
credentials supplied, money deposited, or real order submitted.

## Current implementation

- `app/integrations/kraken.py` reads the fixed public HTTPS OHLC endpoint for the
  canonical `crypto_spot:KRAKEN:BTC:USD` instrument. It has no private/order API.
- Responses undergo instrument, timestamp, continuity, numeric and OHLC checks.
  The final uncommitted candle is always excluded, with an additional explicit
  completion-clock check. Invalid data fails closed.
- `prepare_paper_venue.py` creates a private, non-overwriting Freqtrade configuration:
  dry-run, one spot pair, no exchange keys, loopback API, force entry disabled,
  and an initially stopped `ObservationOnly` strategy that generates no signals.
- The simulated wallet is USD 10,000 and the prospective stake is USD 100, with
  at most one position. These are testing parameters, not real capital approval.
- Existing Yahoo records are not relabelled as Kraken, and crypto data is not
  inserted into the legacy equity-price table or its trusted-provider allowlist.

The [Kraken OHLC documentation](https://docs.kraken.com/api-reference/market-data/get-ohlc-data)
limits recent history; `since` is not a way to obtain arbitrarily old candles.
Training datasets need separately checked historical archives or accumulated
observations. Missing intervals are rejected rather than invented.
The live response on 2026-09-04 contained 721 rows including the unfinished bar;
the client accepts at most 721 total and returns at most 720 committed candles.
Historical retrieval does not itself prove execution freshness; readiness must
separately validate the latest expected completed bar.

## Prepare locally

Use the application's Python environment. Create a private `.paper-venue/`
directory (ignored by Git) and set `FREQTRADE_USERNAME`, `FREQTRADE_PASSWORD`
and `FREQTRADE_JWT_SECRET` using local secret handling. Password and JWT secret
must be distinct, randomly generated and at least 32 characters. These are local
Freqtrade API credentials, **not exchange credentials**.

```sh
python backend/scripts/prepare_paper_venue.py --output .paper-venue/config.json
```

The parent directory must already exist. Output is mode 0600 and an existing file
is never overwritten. Nothing is started by this command.

For a separately installed, reviewed Freqtrade version, run with explicit dry-run:

```sh
freqtrade trade --dry-run --config .paper-venue/config.json \
  --strategy-path backend/freqtrade_strategies --strategy ObservationOnly
```

Check inherited `FREQTRADE__*` environment overrides before starting; Freqtrade's
environment overrides config-file values. Do not add exchange secrets. Do not
reuse a production configuration or ledger. Run in a dedicated working directory
so the configured `trades-observation.dryrun.sqlite` remains isolated. The command
is a manual deployment procedure, not a hardened launcher or certified sandbox.

With the server running locally, set `FREQTRADE_URL=http://127.0.0.1:8080` and use
`python backend/scripts/check_freqtrade.py`. The checker verifies explicit dry-run
mode before reading status. The instance is stopped and strategy has no signals;
this verifies connectivity only, not simulated trade profitability.

## Verified disposable startup

The one-shot `backend/scripts/smoke_freqtrade.py` uses the reviewed official image
`freqtradeorg/freqtrade@sha256:7031bca43ed7668ebf421725dd5016acade6ef88b0771db3e08c96e6d19a42db`.
Pull that exact image before running the script; the script uses `--pull never`.
It runs as the image's non-root user with a read-only root filesystem, resource
limits, no published ports, read-only strategy/adapter mounts and a private tmpfs
configuration/database. Generated API credentials are temporary and visible to
Docker operators via container metadata until removal, not exchange credentials.
No inherited exchange environment variables are forwarded.

The probe checks authenticated API health, explicit dry-run, stopped state,
Kraken/ObservationOnly identity and zero open trades. It removes its exact
randomly named container and temporary database after checking. A successful
2026-09-04 startup reported Freqtrade **2026.8**. The final probe additionally
loaded the project's read-only adapter and successfully called its `ping` and
`status` methods against the real instance (`adapter_verified=true`). All probe
containers were removed; the official image remains cached for reuse. The full
backend suite passes 101 tests, with independent review of both venue packages
and the probe's isolation/cleanup boundaries. This is a connectivity/safety
smoke test, not a running paper service, fill-reconciliation test or model approval.

## Next gate

Implement version-bound model signals,
immutable observed decisions, fill reconciliation and operational checks. Do not
wire the unqualified existing BTC models into trading simply because a service
health check passes. Persistent sidecar deployment and paper execution are not
claimed complete by this configuration/provider/temporary-startup increment.
