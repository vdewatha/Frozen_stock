"""Bounded historical public-trade collection in a standalone research database.

Provider cursor continuity is provenance evidence, not independent proof that
Kraken returned every trade. No missing bars are synthesized; no live DB imports.
"""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import httpx

START = 1735689600  # 2025-01-01T00:00:00Z
END = 1767225600    # 2026-01-01T00:00:00Z
MAX_PAGE = 2 * 1024 * 1024
MAX_RAW = 2 * 1024**3
MAX_PAGES = 100000
MAX_DB = 8 * 1024**3
TABLES = {"backfill_state", "backfill_pages", "backfill_trades", "backfill_hours", "backfill_errors"}


def open_backfill(path):
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()): raise ValueError("Regular standalone database required")
    if path.exists() and path.stat().st_size > MAX_DB: raise ValueError("Database resource cap reached")
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables - TABLES: raise ValueError("Refusing unrelated database")
        db.execute("CREATE TABLE IF NOT EXISTS backfill_state(id INTEGER PRIMARY KEY CHECK(id=1), cursor TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL, last_id INTEGER, last_ts TEXT, raw_bytes INTEGER NOT NULL, pages INTEGER NOT NULL, complete INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS backfill_pages(cursor TEXT PRIMARY KEY, next_cursor TEXT UNIQUE NOT NULL, sha256 TEXT NOT NULL, raw BLOB NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS backfill_trades(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, price TEXT NOT NULL, volume TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS backfill_hours(hour INTEGER PRIMARY KEY, open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL, volume TEXT NOT NULL, count INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS backfill_errors(id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, reason TEXT NOT NULL)")
        row = db.execute("SELECT * FROM backfill_state WHERE id=1").fetchone()
        if row is None:
            if any(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in TABLES - {"backfill_state"}):
                raise ValueError("Missing state in populated backfill database")
            db.execute("INSERT INTO backfill_state VALUES(1,?,?,?,NULL,NULL,0,0,0)", (str(START * 10**9 - 1), START, END))
        elif (row["start"], row["end"]) != (START, END): raise ValueError("Frozen historical range mismatch")
        db.commit()
        return db
    except BaseException:
        db.rollback(); db.close(); raise


def fetch_page(cursor):
    if not isinstance(cursor, str) or not cursor.isdigit() or len(cursor) > 20: raise ValueError("Invalid cursor")
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        with client.stream("GET", "https://api.kraken.com/0/public/Trades",
                           params={"pair": "XXBTZUSD", "since": cursor, "count": 1000}) as response:
            if response.status_code != 200: raise ValueError("Public trade endpoint failed")
            parts, size = [], 0
            for part in response.iter_bytes():
                size += len(part)
                if size > MAX_PAGE: raise ValueError("Public trade page exceeds byte cap")
                parts.append(part)
            return b"".join(parts)


def _amount(value, *, timestamp=False):
    if isinstance(value, bool): raise ValueError("Boolean is not numeric trade data")
    value = Decimal(str(value))
    if not value.is_finite() or value < Decimal("1e-12") or value > (Decimal("1e11") if timestamp else Decimal("1e12")) or value.as_tuple().exponent < -12:
        raise ValueError("Invalid trade amount")
    return value


def ingest_page(db, cursor, raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_PAGE: raise ValueError("Invalid page bytes")
    data = json.loads(raw, parse_float=Decimal)
    if data.get("error") != [] or set(data.get("result", {})) != {"XXBTZUSD", "last"}: raise ValueError("Unexpected public trade response")
    result = data["result"]
    following, rows = result["last"], result["XXBTZUSD"]
    if not isinstance(following, str) or not following.isdigit() or len(following) > 20 or int(following) <= int(cursor):
        raise ValueError("Cursor failed to advance")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000: raise ValueError("Invalid page trade count")
    parsed = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 7 or type(row[6]) is not int or not 0 < row[6] < 2**63 or row[3] not in ("b", "s") or row[4] not in ("l", "m") or not isinstance(row[5], str):
            raise ValueError("Invalid trade record schema")
        price, volume, ts = _amount(row[0]), _amount(row[1]), _amount(row[2], timestamp=True)
        if ts < START or ts > END + 86400: raise ValueError("Trade outside bounded collection window")
        parsed.append((row[6], ts, price, volume))
    if any(b[0] <= a[0] or b[1] < a[1] for a, b in zip(parsed, parsed[1:])):
        raise ValueError("Trade ordering invalid")
    # JSON trade timestamps may be rounded to microseconds, unlike nanosecond
    # cursors. Do not reject a legitimate sub-microsecond rounding difference.
    if Decimal(following) / 10**9 + Decimal("0.000001") < parsed[-1][1]: raise ValueError("Cursor precedes trade timestamp")
    digest = hashlib.sha256(raw).hexdigest()
    if db.in_transaction: raise ValueError("Fresh transaction required")
    db.execute("BEGIN IMMEDIATE")
    try:
        duplicate = db.execute("SELECT sha256 FROM backfill_pages WHERE cursor=?", (cursor,)).fetchone()
        if duplicate:
            if duplicate[0] != digest: raise ValueError("Previously recorded page was revised")
            db.commit(); return {"status": "duplicate"}
        state = db.execute("SELECT * FROM backfill_state WHERE id=1").fetchone()
        if state["complete"] or cursor != state["cursor"]: raise ValueError("Unexpected persisted cursor")
        if state["pages"] >= MAX_PAGES or state["raw_bytes"] + len(raw) > MAX_RAW: raise ValueError("Collection resource cap reached")
        last_id, last_ts = state["last_id"], Decimal(state["last_ts"]) if state["last_ts"] else None
        inserted = 0
        for trade_id, ts, price, volume in parsed:
            existing = db.execute("SELECT ts,price,volume FROM backfill_trades WHERE id=?", (trade_id,)).fetchone()
            values = tuple(str(v) for v in (ts, price, volume))
            if existing:
                if tuple(Decimal(v) for v in existing) != (ts, price, volume): raise ValueError("Duplicate trade was revised")
                continue
            if last_id is not None and (trade_id <= last_id or ts < last_ts): raise ValueError("Cross-page trade ordering invalid")
            db.execute("INSERT INTO backfill_trades VALUES(?,?,?,?)", (trade_id, *values))
            inserted += 1
            last_id, last_ts = trade_id, ts
            if ts >= END: continue
            hour = int(ts // 3600) * 3600
            current = db.execute("SELECT * FROM backfill_hours WHERE hour=?", (hour,)).fetchone()
            if current:
                db.execute("UPDATE backfill_hours SET high=?,low=?,close=?,volume=?,count=count+1 WHERE hour=?",
                    (str(max(Decimal(current["high"]), price)), str(min(Decimal(current["low"]), price)), str(price), str(Decimal(current["volume"]) + volume), hour))
            else:
                db.execute("INSERT INTO backfill_hours VALUES(?,?,?,?,?,?,1)", (hour, str(price), str(price), str(price), str(price), str(volume)))
        if last_ts is None or inserted == 0: raise ValueError("No advancing trade evidence")
        db.execute("INSERT INTO backfill_pages VALUES(?,?,?,?)", (cursor, following, digest, raw))
        complete = last_ts >= END
        db.execute("UPDATE backfill_state SET cursor=?,last_id=?,last_ts=?,raw_bytes=raw_bytes+?,pages=pages+1,complete=? WHERE id=1",
                   (following, last_id, str(last_ts), len(raw), int(complete)))
        db.commit()
        return {"status": "complete" if complete else "collected", "cursor": following, "page_sha256": digest}
    except BaseException:
        db.rollback(); raise


def export_hourly(db, output, *, gap_audit=None):
    """Publish only after end coverage and every expected hour exist; no gap fill."""
    import csv, io
    state = db.execute("SELECT * FROM backfill_state WHERE id=1").fetchone()
    rows = db.execute("SELECT * FROM backfill_hours ORDER BY hour").fetchall()
    missing, audit_hash = [], None
    if gap_audit is not None:
        from app.services.kraken_gap_audit import verify_audit
        missing, audit_hash = verify_audit(db, gap_audit)
    expected = [hour for hour in range(START, END, 3600) if hour not in missing]
    if not state["complete"] or [r["hour"] for r in rows] != expected:
        raise ValueError("Full interval incomplete or contains missing trade hours")
    # Verify immutable raw provenance before publishing any derived data.
    chain = hashlib.sha256()
    expected_cursor, page_count, raw_bytes = str(START * 10**9 - 1), 0, 0
    rebuilt, latest_id = {}, None
    for row in db.execute("SELECT cursor,next_cursor,sha256,raw FROM backfill_pages ORDER BY length(cursor),cursor"):
        if hashlib.sha256(row["raw"]).hexdigest() != row["sha256"]: raise ValueError("Persisted raw page integrity failure")
        if row["cursor"] != expected_cursor: raise ValueError("Persisted cursor chain gap")
        expected_cursor, page_count, raw_bytes = row["next_cursor"], page_count + 1, raw_bytes + len(row["raw"])
        result = json.loads(row["raw"], parse_float=Decimal)["result"]
        if result["last"] != row["next_cursor"]: raise ValueError("Persisted cursor identity changed")
        for trade in result["XXBTZUSD"]:
            ident, ts = trade[6], Decimal(str(trade[2]))
            if latest_id is not None and ident <= latest_id: continue  # Recorded overlap, already validated at ingestion.
            latest_id = ident
            if ts >= END: continue
            price, volume = Decimal(trade[0]), Decimal(trade[1])
            hour = int(ts // 3600) * 3600
            if hour not in rebuilt: rebuilt[hour] = [price, price, price, price, volume, 1]
            else:
                aggregate = rebuilt[hour]
                aggregate[1], aggregate[2], aggregate[3] = max(aggregate[1], price), min(aggregate[2], price), price
                aggregate[4] += volume
                aggregate[5] += 1
        chain.update((row["cursor"] + ":" + row["next_cursor"] + ":" + row["sha256"] + "\n").encode())
    if (expected_cursor, page_count, raw_bytes) != (state["cursor"], state["pages"], state["raw_bytes"]):
        raise ValueError("Persisted cursor accounting mismatch")
    if any(rebuilt.get(row["hour"]) != [Decimal(row[k]) for k in ("open", "high", "low", "close", "volume", "count")] for row in rows):
        raise ValueError("Aggregated candles differ from preserved raw trades")
    handle = io.StringIO(newline="")
    writer = csv.writer(handle)
    for row in rows:
        writer.writerow([row["hour"], *[row[k] for k in ("open", "high", "low", "close", "volume", "count")]])
    payload = handle.getvalue().encode()
    provenance = {"format": "kraken-public-trades-backfill-v1", "source_url": "https://api.kraken.com/0/public/Trades",
        "instrument": "crypto_spot:KRAKEN:BTC:USD", "start": START, "end": END, "pages": state["pages"],
        "raw_bytes": state["raw_bytes"], "page_chain_sha256": chain.hexdigest(), "csv_sha256": hashlib.sha256(payload).hexdigest(),
        "rows": len(rows), "aggregation": "ordered-public-trades-no-gap-fill", "eligible_for_trading": False}
    provenance_bytes = json.dumps(provenance, sort_keys=True, indent=2).encode()
    if missing:
        provenance.update(format='kraken-public-trades-backfill-v2', gap_policy='segment',
            missing_hours=missing, calendar_hours=(END-START)//3600, gap_audit_sha256=audit_hash)
        provenance_bytes = json.dumps(provenance, sort_keys=True, indent=2).encode()
    source_claim = "kraken-public-trades-backfill:" + hashlib.sha256(provenance_bytes).hexdigest()
    for path, content in ((Path(output), payload), (Path(output).with_name("provenance.json"), provenance_bytes)):
        if path.is_symlink(): raise ValueError("No symlink export")
        if path.exists():
            if not path.is_file() or path.read_bytes() != content: raise ValueError("Immutable export conflict")
        else:
            import os, tempfile
            fd, temporary = tempfile.mkstemp(prefix=".backfill-export-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Hard-link publication is atomic and cannot replace a prior
                # artifact. A restart can complete the second artifact safely.
                os.link(temporary, path)
            finally:
                os.unlink(temporary)
    return {"status": "exported", "rows": len(rows), "source_claim": source_claim, "eligible_for_trading": False}
