# Instrument and candle contracts

`services/instruments.py` defines immutable identities for US equities, US ETFs,
and crypto spot. Identity includes asset class, venue, base and quote. Crypto
must supply both currencies; symbol-only values cannot identify a pair.
Venue is a four-letter MIC for US securities; this format check does not verify
the venue exists or that the security is listed there. Provider instrument
catalog validation remains required. Crypto exchanges use explicit uppercase
venue identifiers. Different venues and quote currencies remain different IDs.

Supported fixed intervals are 1m, 5m, 15m, 1h, 4h and 1d. Candle timestamps must
be timezone-aware and are normalized to UTC. OHLC and volume are checked for
finite values and coherent bounds. `require_complete(as_of=...)` rejects a bar
until its fixed interval has ended, using an explicit caller clock.

This is WP06's pure contract component. It does not migrate existing symbol-only
tables, verify provider catalogs, establish source trust, detect missing bars,
or validate US sessions/holidays. Official exchange calendars and provider bar
semantics must be integrated before using fixed-interval completion as a full
equity trading-data gate. US daily bars are session bars, not necessarily
24-hour intervals; adapters must not pretend this contract supplies a calendar.
