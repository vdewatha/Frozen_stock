# Durable Kraken hourly observations

`collect_kraken(db, client=None, as_of=None)` stores completed public BTC/USD spot candles in isolated `crypto_candles`, never legacy equity tables. It returns a `CollectionRun`: `success` or `blocked`, with fetched/inserted counts and sanitized error code. The caller must commit the transaction to retain both observations and audit; rollback removes both. Network/database infrastructure failures can still raise and must trigger operational alerts.

Apply the standard Alembic migrations before use. Schedule collection shortly after each UTC hour. Kraken supplies only its recent rolling window; this is continuous accumulation, not a historical backfill service. A collection outage longer than the available window creates a blocked gap that needs an independently vetted historical recovery source, not synthetic candles.

Identity is `crypto_spot:KRAKEN:BTC:USD` / `1h`. The unique interval key plus conflict-safe insertion makes repeat and concurrent polling idempotent. Changed historical content blocks the entire incoming batch rather than overwriting observations. SHA-256 covers canonical identity, time, and 12-decimal OHLCV. `observed_at` records when this service first received each candle; provider historical timestamps do not establish forward observation evidence.

`load_closed_history(db, as_of=aware_datetime, minimum=100)` returns ordered ORM candle rows after checking hashes, hourly continuity, latest completed hour, and observation availability. It raises `CryptoDataError` on failure. This service deliberately fails on any stored gap or corruption. No trusted status, backtest result, or profitable model is inferred from successful collection.

SQLite and PostgreSQL conflict handling are supported; automated concurrency and transaction tests use temporary SQLite. PostgreSQL production verification remains necessary. Application immutability does not prevent database administrators changing rows; hashes detect accidental OHLCV modification, not malicious coordinated rewriting.
