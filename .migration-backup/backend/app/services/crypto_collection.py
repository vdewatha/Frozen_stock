"""Caller-transaction-owned public hourly collection; never revises history."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from app.integrations.kraken import BTC_USD, KrakenPublicClient
from app.models.crypto_data import CollectionRun, CryptoCandle
from app.services.instruments import Candle, Timeframe, utc_timestamp


class CryptoDataError(ValueError):
    pass


def _stamp(value):
    return utc_timestamp(value).isoformat()


def _values(candle):
    values = {name: Decimal(str(getattr(candle, name))).quantize(Decimal("0.000000000001"))
              for name in ("open", "high", "low", "close", "volume")}
    return dict(instrument_id=BTC_USD.instrument_id, timeframe="1h", opened_at=_stamp(candle.opened_at), **values)


def _hash(values):
    return hashlib.sha256(json.dumps({k: str(v) for k, v in values.items()}, sort_keys=True).encode()).hexdigest()


def _rows(db):
    return list(db.scalars(select(CryptoCandle).where(CryptoCandle.instrument_id == BTC_USD.instrument_id,
                     CryptoCandle.timeframe == "1h").order_by(CryptoCandle.opened_at)).all())


def _check(rows, clock, minimum):
    if len(rows) < minimum:
        raise CryptoDataError("insufficient_history")
    expected = clock.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    previous = None
    for row in rows:
        opened = utc_timestamp(datetime.fromisoformat(row.opened_at))
        if utc_timestamp(datetime.fromisoformat(row.observed_at)) > clock:
            raise CryptoDataError("observation_not_available")
        if opened.minute or opened.second or opened.microsecond or opened > expected:
            raise CryptoDataError("future_or_unaligned_candle")
        if previous is not None and opened - previous != timedelta(hours=1):
            raise CryptoDataError("history_gap")
        candle = Candle(BTC_USD, Timeframe.HOUR, opened, *[float(getattr(row, name)) for name in ("open", "high", "low", "close", "volume")])
        if row.content_sha256 != _hash(_values(candle)):
            raise CryptoDataError("stored_integrity_failure")
        previous = opened
    if previous != expected:
        raise CryptoDataError("stale_history")


def load_closed_history(db, *, as_of, minimum=100):
    if type(minimum) is not int or minimum < 1:
        raise CryptoDataError("invalid_minimum")
    clock = utc_timestamp(as_of)
    rows = _rows(db)
    _check(rows, clock, minimum)
    return rows


def collect_kraken(db, client=None, as_of=None):
    clock = utc_timestamp(as_of if as_of is not None else datetime.now(timezone.utc))
    run = CollectionRun(instrument_id=BTC_USD.instrument_id, timeframe="1h", observed_at=_stamp(clock),
                        status="blocked", fetched_count=0, inserted_count=0, error_code=None)
    owned = client is None
    client = client or KrakenPublicClient()
    try:
        candles = client.ohlc(BTC_USD, Timeframe.HOUR, as_of=clock)
        run.fetched_count = len(candles)
        incoming = []
        for candle in candles:
            if candle.instrument != BTC_USD or candle.timeframe != Timeframe.HOUR:
                raise CryptoDataError("instrument_mismatch")
            values = _values(candle)
            incoming.append(CryptoCandle(**values, observed_at=_stamp(clock), content_sha256=_hash(values)))
        _check(incoming, clock, 1)
        # SAVEPOINT rolls back the entire batch on changed history. A conflict-safe
        # insert serializes concurrent writers, and re-reading verifies the winner.
        connection = db.connection()
        if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN")
        with db.begin_nested():
            dialect = db.get_bind().dialect.name
            insert = {"sqlite": sqlite_insert, "postgresql": postgres_insert}.get(dialect)
            if insert is None:
                raise CryptoDataError("unsupported_database")
            for row in incoming:
                values = {c.name: getattr(row, c.name) for c in CryptoCandle.__table__.columns if c.name != "id"}
                result = db.execute(insert(CryptoCandle).values(**values).on_conflict_do_nothing(
                    index_elements=["instrument_id", "timeframe", "opened_at"]))
                run.inserted_count += result.rowcount
                stored = db.scalar(select(CryptoCandle).where(CryptoCandle.instrument_id == row.instrument_id,
                                  CryptoCandle.timeframe == row.timeframe, CryptoCandle.opened_at == row.opened_at))
                if stored.content_sha256 != row.content_sha256:
                    raise CryptoDataError("provider_revision")
            _check(_rows(db), clock, 1)
        run.status = "success"
    except CryptoDataError as exc:
        run.error_code = str(exc)
        run.inserted_count = 0
    except (ValueError, RuntimeError):
        run.error_code = "provider_or_validation_failure"
        run.inserted_count = 0
    finally:
        if owned:
            client.close()
    db.add(run)
    db.flush()
    return run
