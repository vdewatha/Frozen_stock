"""Strict offline Kraken OHLCVT research snapshots, never forward evidence."""
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile

import pandas as pd
from app.services.crypto_collection import _check
from app.services.instruments import utc_timestamp

SOURCE_URL = "https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data"
INSTRUMENT = "crypto_spot:KRAKEN:BTC:USD"
FIELDS = ("open", "high", "low", "close", "volume")
MAX_BYTES = 64 * 1024 * 1024


def _json(value):
    return json.dumps(value, sort_keys=True, indent=2, allow_nan=False).encode()


def _decimal(value):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or n < 0 or n > Decimal("1000000000000") or n != n.quantize(Decimal("0.000000000001")):
            raise ValueError("Invalid numeric precision or range")
        return n
    except InvalidOperation:
        raise ValueError("Invalid numeric field") from None


def _validate(rows, clock):
    previous = None
    for row in rows:
        date = row["date"]
        if date.minute or date.second or date.microsecond or date + timedelta(hours=1) > clock:
            raise ValueError("Unaligned or future/incomplete candle")
        if previous is not None and date - previous != timedelta(hours=1):
            raise ValueError("Duplicate, unordered, or missing hourly interval; no filling allowed")
        values = [_decimal(row[k]) for k in FIELDS]
        op, high, low, close, volume = values
        if min(op, high, low, close) <= 0 or low > min(op, close) or high < max(op, close) or high < low:
            raise ValueError("Invalid OHLC bounds")
        previous = date
    if not rows:
        raise ValueError("Empty hourly history")


def _bound(value):
    if value is None:
        return None
    result = utc_timestamp(datetime.fromisoformat(value) if isinstance(value, str) else value)
    if result.minute or result.second or result.microsecond:
        raise ValueError("Selection bounds must be UTC-aware aligned hours")
    return result


def read_kraken_csv(path, *, as_of=None, start=None, end=None, allowed_missing_hours=None):
    """Return a validated Decimal-valued frame and raw file hash.

    Headerless schema is timestamp,open,high,low,close,volume,trades. Timestamp
    is UNIX seconds. Filename is an additional check, not proof of authenticity.
    """
    path = Path(path)
    if path.name != "XBTUSD_60.csv" or path.is_symlink():
        raise ValueError("Expected regular XBTUSD_60.csv for Kraken BTC/USD hourly")
    clock = utc_timestamp(as_of if as_of is not None else datetime.now(timezone.utc))
    start, end = _bound(start), _bound(end)
    if (start is None) != (end is None) or (start is not None and (start >= end or end > clock)):
        raise ValueError("Provide both start and exclusive end, ordered and not in the future")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("Input must be a regular CSV no larger than 64 MiB")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Input exceeds size limit")
    rows = []
    for values in csv.reader(io.StringIO(raw.decode("utf-8"))):
        if len(values) != 7 or not values[0].isascii() or not values[0].isdigit() or not values[6].isascii() or not values[6].isdigit():
            raise ValueError("Expected seven headerless OHLCVT fields with integer timestamp/trades")
        try:
            opened = datetime.fromtimestamp(int(values[0]), timezone.utc)
        except (OverflowError, OSError):
            raise ValueError("Invalid timestamp") from None
        row = dict(date=opened, **{key: _decimal(value) for key, value in zip(FIELDS, values[1:6])})
        # Validate every raw row's numerical bounds/alignment, but permit known
        # no-trade archive gaps outside an explicitly selected research window.
        _validate([row], clock)
        if start is None or start <= opened < end:
            rows.append(row)
    if allowed_missing_hours is None:
        _validate(rows, clock)
    else:
        if start is None or not isinstance(allowed_missing_hours, list) or not 1 <= len(allowed_missing_hours) <= 24:
            raise ValueError('Explicit bounded missing-hour list and calendar selection required')
        if any(type(t) is not int or t % 3600 for t in allowed_missing_hours):
            raise ValueError('Missing hours must be aligned UTC epoch integers')
        actual = [int(row['date'].timestamp()) for row in rows]
        expected = list(range(int(start.timestamp()), int(end.timestamp()), 3600))
        if (allowed_missing_hours != sorted(set(allowed_missing_hours))
                or sorted(set(expected) - set(actual)) != allowed_missing_hours
                or actual != sorted(set(actual)) or set(actual) - set(expected)):
            raise ValueError('Actual missing intervals differ from explicit gap policy')
        if not rows:
            raise ValueError('Empty hourly history')
    if start is not None and (rows[0]["date"] != start or rows[-1]["date"] + timedelta(hours=1) != end):
        raise ValueError("Selected window boundary candles are missing")
    return pd.DataFrame(rows), hashlib.sha256(raw).hexdigest()


def merge_verified_history(history, live_rows, *, as_of):
    """Append verified observations only with exact overlap; never repair gaps."""
    clock = utc_timestamp(as_of)
    rows = history.to_dict("records")
    _validate(rows, clock)
    live_rows = list(live_rows)
    _check(live_rows, clock, 1)
    if any(row.instrument_id != INSTRUMENT or row.timeframe != "1h" for row in live_rows):
        raise ValueError("Live observation identity mismatch")
    by_date = {r["date"]: r for r in rows}
    overlap = 0
    for live in live_rows:
        date = utc_timestamp(datetime.fromisoformat(live.opened_at))
        row = dict(date=date, **{key: _decimal(getattr(live, key)) for key in FIELDS})
        if date in by_date:
            overlap += 1
            if any(_decimal(by_date[date][key]) != row[key] for key in FIELDS):
                raise ValueError("Historical/live overlap conflict; revision requires independent review")
        else:
            by_date[date] = row
    if not overlap:
        raise ValueError("At least one exact historical/live overlapping candle is required")
    merged = sorted(by_date.values(), key=lambda row: row["date"])
    _validate(merged, clock)
    return pd.DataFrame(merged)


def import_kraken_history(path, output, *, source_url, as_of=None, live_rows=None, minimum_days=365, start=None, end=None):
    if source_url != SOURCE_URL:
        raise ValueError("Explicit official Kraken OHLCVT source-page provenance is required")
    if type(minimum_days) is not int or not 1 <= minimum_days <= 10000:
        raise ValueError("minimum_days must be 1..10000")
    clock = utc_timestamp(as_of if as_of is not None else datetime.now(timezone.utc))
    frame, source_hash = read_kraken_csv(path, as_of=clock, start=start, end=end)
    provenance = []
    if live_rows is not None:
        live_rows = list(live_rows)
        frame = merge_verified_history(frame, live_rows, as_of=clock)
        provenance = [{"opened_at": r.opened_at, "observed_at": r.observed_at,
                       "content_sha256": r.content_sha256} for r in live_rows]
    if len(frame) < minimum_days * 24:
        raise ValueError("Insufficient contiguous hourly history for requested minimum_days")
    frame["date"] = frame["date"].map(lambda d: d.isoformat())
    for field in FIELDS:
        frame[field] = frame[field].map(lambda n: format(_decimal(n).normalize(), "f"))
    snapshot = frame.to_csv(index=False).encode()
    identity = {"format_version": 2, "instrument_id": INSTRUMENT, "timeframe_minutes": 60,
        "selection_start": _bound(start).isoformat() if start is not None else None,
        "selection_end_exclusive": _bound(end).isoformat() if end is not None else None,
        "source_url": source_url, "source_filename": Path(path).name, "source_sha256": source_hash,
        "dataset_sha256": hashlib.sha256(snapshot).hexdigest(),
        "verified_live_overlap_sha256": hashlib.sha256(_json(provenance)).hexdigest() if provenance else None}
    snapshot_id = hashlib.sha256(_json(identity)).hexdigest()
    manifest = dict(identity, snapshot_id=snapshot_id, imported_at=clock.isoformat(), rows=len(frame),
        start=frame.iloc[0]["date"], end=frame.iloc[-1]["date"], status="research_only",
        eligible_for_qualification=False, provenance_authenticated=False,
        limitations=["Caller source claim and file hash are not vendor authentication",
                      "Historical records are never forward-observed evidence", "No missing candles were fabricated"],
        files={"dataset.csv": identity["dataset_sha256"]})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / snapshot_id
    if destination.exists():
        raise FileExistsError("Immutable history snapshot already exists")
    with tempfile.TemporaryDirectory(prefix=".history-", dir=output) as temporary:
        staging = Path(temporary)
        (staging / "dataset.csv").write_bytes(snapshot)
        if provenance:
            payload = _json(provenance)
            (staging / "live_provenance.json").write_bytes(payload)
            manifest["files"]["live_provenance.json"] = hashlib.sha256(payload).hexdigest()
        (staging / "manifest.json").write_bytes(_json(manifest))
        os.rename(staging, destination)
    return manifest


def load_history_bundle(directory):
    """Verify immutable bundle identity and content; never deserialize code."""
    directory = Path(directory)
    if directory.is_symlink() or ".." in directory.parts:
        raise ValueError("Unsafe history bundle path")
    def read(name):
        path = directory / name
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ValueError("Invalid history artifact")
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("History artifact size exceeded")
        return raw
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result
    try:
        manifest = json.loads(read("manifest.json"), object_pairs_hook=unique)
        keys = ("format_version", "instrument_id", "timeframe_minutes", "source_url", "source_filename",
                "source_sha256", "dataset_sha256", "verified_live_overlap_sha256")
        identity = {key: manifest[key] for key in keys}
        if identity["format_version"] == 2:
            identity.update({key: manifest[key] for key in ("selection_start", "selection_end_exclusive")})
            start, end = _bound(identity["selection_start"]), _bound(identity["selection_end_exclusive"])
            if (start is None) != (end is None) or (start is not None and start >= end):
                raise ValueError("Invalid research selection bounds")
        ident = hashlib.sha256(_json(identity)).hexdigest()
        if (type(identity["format_version"]) is not int or identity["format_version"] not in (1, 2)
            or identity["instrument_id"] != INSTRUMENT or type(identity["timeframe_minutes"]) is not int
            or identity["timeframe_minutes"] != 60 or identity["source_url"] != SOURCE_URL
            or identity["source_filename"] != "XBTUSD_60.csv" or ident != manifest["snapshot_id"]
            or directory.name != ident or manifest["status"] != "research_only"
            or manifest["eligible_for_qualification"] is not False or manifest["provenance_authenticated"] is not False):
            raise ValueError("History identity mismatch")
        expected = {"dataset.csv"} | ({"live_provenance.json"} if identity["verified_live_overlap_sha256"] else set())
        if set(manifest["files"]) != expected or {p.name for p in directory.iterdir()} != expected | {"manifest.json"}:
            raise ValueError("Unexpected history artifacts")
        contents = {name: read(name) for name in expected}
        for name, data in contents.items():
            if hashlib.sha256(data).hexdigest() != manifest["files"][name]:
                raise ValueError("History checksum mismatch")
        if (manifest["files"]["dataset.csv"] != identity["dataset_sha256"]
            or ("live_provenance.json" in expected and manifest["files"]["live_provenance.json"] != identity["verified_live_overlap_sha256"])):
            raise ValueError("History content identity mismatch")
        frame = pd.read_csv(io.BytesIO(contents["dataset.csv"]), dtype=str, keep_default_na=False)
        if list(frame.columns) != ["date", *FIELDS]:
            raise ValueError("Unexpected history columns")
        rows = []
        for row in frame.to_dict("records"):
            rows.append(dict(date=utc_timestamp(datetime.fromisoformat(row["date"])),
                             **{key: _decimal(row[key]) for key in FIELDS}))
        clock = utc_timestamp(datetime.fromisoformat(manifest["imported_at"]))
        if clock > datetime.now(timezone.utc):
            raise ValueError("Future history import")
        _validate(rows, clock)
        if identity["format_version"] == 2 and start is not None:
            if end > clock or rows[0]["date"] > start or rows[-1]["date"] + timedelta(hours=1) < end:
                raise ValueError("Dataset does not cover selected research window")
        if (type(manifest["rows"]) is not int or len(rows) != manifest["rows"]
            or rows[0]["date"].isoformat() != manifest["start"] or rows[-1]["date"].isoformat() != manifest["end"]):
            raise ValueError("History span mismatch")
        return pd.DataFrame(rows), manifest
    except (OSError, KeyError, TypeError, OverflowError) as exc:
        raise ValueError("Invalid history bundle") from exc
