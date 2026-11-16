# Research data snapshots

Research scripts and the app used to read history through different paths, and a result recorded only its
`data_end`. A snapshot gives a data folder an identity, states what it holds and lacks, and lets an experiment
record exactly which data it was computed on. The code is `app/services/research_data.py`.

## What a snapshot is

A folder with a `SNAPSHOT.json` manifest:

- `snapshot_id`: SHA-256 over the name, layout, sources, contract and every file's path, size and hash.
- `contract`: for each of seven parts, whether the folder provides it (`provided`, `partial`, `absent`) and a
  note saying how. The parts are security identity, symbol changes, daily bars, corporate actions, terminal
  payouts, event availability and coverage exclusions.
- `files`: path, bytes and SHA-256 of every file.

A snapshot never claims what it lacks. Missing delisting returns are `absent`, and `Snapshot.terminal()` then
reports the payout as unknown with the brackets a sensitivity run should use (0%, -50%, -100% of the last close).

## Commands

Run from `artifacts/api-server/backend`.

```bash
python scripts/research_snapshot.py presets
```

```bash
python scripts/research_snapshot.py build ~/.paper-training/dip-rules-full-market-20261112
```

```bash
python scripts/research_snapshot.py verify ~/.paper-training/dip-rules-full-market-20261112 --full
```

```bash
python scripts/research_snapshot.py list
```

`build` on a folder named like a preset uses that preset's contract. Any other folder needs
`--contract contract.json` with a status and a note for each part.

## The five existing caches

The caches the 2026-11-11 to 11-14 studies wrote keep their own layout (`legacy:<script>`). They received a
manifest on 2026-11-16, after the studies ran, so each id identifies the cache as found. The hypothesis register
names these ids on the studies that used them.

| Cache | Snapshot id (first 16) |
|---|---|
| dip-rules-full-market-20261112 | `6ddf921908c93135` |
| earnings-drift-delisted-20261111 | `cb6f332389ecdf16` |
| public-information-20261114 | `8cc251eb1487a8ca` |
| stocks-in-play-breakout-20261112 | `2dc1aaaea1cc1aae` |
| spy-intraday-momentum-20261112 | `abd6fea2c60df805` |

## Writing new research data

Write it in the canonical layout with `research_data.write_canonical(...)`: `securities.csv`,
`symbol_history.csv`, `bars/<security_id>.csv.gz` (raw OHLCV plus an adjusted close), and optionally
`corporate_actions.csv`, `events.csv` (with `available_at` in UTC) and `exclusions.csv`. The contract is derived
from what was written. Read it back with `research_data.open_snapshot(id_or_path)`.

A study records `research_data.dataset_ref(folder)` in its `results.json` under `dataset`, and adds the id to
its entry in `app/services/data/hypothesis-register.json`.

## The app's replays

Strategy discovery and the experiment engine record the data they used on every run (`dataset` in the run row
and in each experiment row).

- With `RESEARCH_SNAPSHOT_ID` unset, which is production, they use the app's cached yfinance history and record
  a reference that says so and lists that path's known gaps.
- With `RESEARCH_SNAPSHOT_ID` set to a canonical snapshot's id, they replay that snapshot through
  `Snapshot.loader()` and record its id. `RESEARCH_SNAPSHOT_ROOT` says where snapshot folders live (default
  `~/.paper-training`). A configured snapshot that cannot be opened blocks the run. It is never swapped for
  other data.

## Still open

No source in the $0 data budget gives delisting returns, so terminal payouts remain `absent` in every existing
cache. The legacy caches have no permanent security id. Converting the whole-market bar cache to the canonical
layout is the step that would let the app's replays and the research scripts run on identical data.
