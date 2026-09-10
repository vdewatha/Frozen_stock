# Public-trade historical fallback

`backend/scripts/backfill_kraken_trades.py --output-directory PRIVATE_DIRECTORY
--max-pages-this-run 100000` collects only Kraken BTC/USD public trades for
2025-01-01 through 2026-01-01. Do not point it at a live database. The worker has
not been started by its implementation tests.

Kraken documents a maximum of 1,000 trades per request and a `since` cursor at
[Get Recent Trades](https://docs.kraken.com/api-reference/market-data/get-recent-trades).
The worker uses this fixed public HTTPS endpoint without credentials, redirects,
or environment proxies. It waits at least one second between requests and backs
off on errors, stopping after five consecutive failures. SIGINT/SIGTERM preserves
progress. Each invocation has an explicit page budget; rerunning resumes the
committed cursor. Default invocation budget is 1,000 pages.

`trade-backfill.sqlite` contains frozen interval state, original response bytes
and hashes, cursor links, deduplicated trade IDs, and provisional hourly
aggregates. A transaction commits each page and its derived state together.
Duplicates cannot double count; revised trades/pages or backwards ordering block
collection. Missing hours are never filled. Provisional aggregate rows are not
published as complete candles.

Resource limits: 2 MiB per response, 2 GiB original-response storage, 100,000 pages,
8 GiB database. Hitting a cap stops collection for operator review. Limits do not
promise a full year fits; market activity determines size and duration. The local
SQLite file stores trades as well as response bodies, so provision adequate disk.

After a trade at or beyond the interval end is observed, export requires all
8,760 hourly intervals. It rechecks raw hashes, cursor continuity, and rebuilds
aggregates from preserved responses before publishing headerless `XBTUSD_60.csv`.
The file layout is compatible with the strict local CSV reader, but its provenance
is **public Trades API aggregation**, not Kraken's downloadable OHLCVT archive.
`provenance.json` records the true endpoint, interval, ordered page-chain hash,
CSV hash, and counts. Training must use the returned
`kraken-public-trades-backfill:<provenance-hash>` source claim.

Exports are immutable and individually atomically published. An interrupted
export can resume without replacing existing evidence. Cursor continuity is
provider provenance, not independent proof that the provider omitted no trades.
No model is trained, bound, promoted, or traded by this worker.
