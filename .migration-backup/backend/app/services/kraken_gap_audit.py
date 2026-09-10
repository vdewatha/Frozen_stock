"""Evidence for bounded no-trade gaps; never creates prices or candles."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from app.services.kraken_trade_backfill import START, END, fetch_page


def missing_hours(db):
    present = {r[0] for r in db.execute('SELECT hour FROM backfill_hours')}
    return sorted(set(range(START, END, 3600)) - present)


def _boundary(db, hour, raw):
    data = json.loads(raw, parse_float=Decimal)
    if data.get('error') != [] or set(data.get('result', {})) != {'XXBTZUSD', 'last'}:
        raise ValueError('Invalid gap probe response')
    trades = data['result']['XXBTZUSD']
    if not isinstance(trades, list) or not trades or len(trades) > 1000:
        raise ValueError('Invalid gap probe trades')
    first = trades[0]
    if len(first) != 7 or type(first[6]) is not int:
        raise ValueError('Invalid first trade identity')
    after = db.execute('SELECT id,ts,price,volume FROM backfill_trades WHERE id=?', (first[6],)).fetchone()
    before = db.execute('SELECT id,ts,price,volume FROM backfill_trades WHERE id=?', (first[6]-1,)).fetchone()
    if after is None or before is None:
        raise ValueError('Gap boundaries require consecutive stored trade IDs')
    if (Decimal(str(first[2])), Decimal(first[0]), Decimal(first[1])) != tuple(Decimal(v) for v in after[1:]):
        raise ValueError('Fresh probe conflicts with saved trade evidence')
    if not Decimal(before[1]) < hour < hour + 3600 <= Decimal(after[1]) <= hour + 86400:
        raise ValueError('Probe does not establish a bounded empty hour')
    return {'before': list(before), 'after': list(after)}


def build_audit(db, *, fetcher=fetch_page):
    gaps = missing_hours(db)
    if not 1 <= len(gaps) <= 24:
        raise ValueError('Gap audit supports 1..24 absent hours only')
    probes = []
    for hour in gaps:
        cursor = str(hour * 10**9)
        raw = fetcher(cursor)
        if not isinstance(raw, bytes) or len(raw) > 2 * 1024 * 1024:
            raise ValueError('Invalid bounded probe bytes')
        probes.append({'hour': hour, 'since': cursor, 'response': raw.decode(),
            'response_sha256': hashlib.sha256(raw).hexdigest(), **_boundary(db, hour, raw)})
    return {'format': 'kraken-no-trade-gap-audit-v1', 'instrument': 'crypto_spot:KRAKEN:BTC:USD',
        'source_url': 'https://api.kraken.com/0/public/Trades', 'checked_at': datetime.now(timezone.utc).isoformat(),
        'missing_hours': gaps, 'probes': probes, 'eligible_for_trading': False,
        'limitation': 'Fresh API plus consecutive provider trade IDs; not independent proof of exchange completeness'}


def verify_audit(db, path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Expected bounded regular gap audit')
    raw = path.read_bytes()
    audit = json.loads(raw)
    gaps = missing_hours(db)
    if (audit.get('format') != 'kraken-no-trade-gap-audit-v1'
            or audit.get('instrument') != 'crypto_spot:KRAKEN:BTC:USD'
            or audit.get('source_url') != 'https://api.kraken.com/0/public/Trades'
            or audit.get('eligible_for_trading') is not False
            or audit.get('missing_hours') != gaps or not 1 <= len(gaps) <= 24
            or [p['hour'] for p in audit['probes']] != gaps):
        raise ValueError('Gap audit identity or coverage mismatch')
    for probe in audit['probes']:
        response = probe['response'].encode()
        if probe['since'] != str(probe['hour'] * 10**9) or hashlib.sha256(response).hexdigest() != probe['response_sha256']:
            raise ValueError('Gap probe integrity mismatch')
        boundary = _boundary(db, probe['hour'], response)
        if boundary != {key: probe[key] for key in ('before', 'after')}:
            raise ValueError('Gap boundary identity changed')
    return gaps, hashlib.sha256(raw).hexdigest()
