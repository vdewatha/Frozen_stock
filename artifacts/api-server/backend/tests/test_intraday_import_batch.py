"""The scheduled import stores the newest window of every symbol in one transaction.

On 2026-11-16 a 47-symbol run took 84-102 s on the hosted database (about
0.1 s a round trip, about 13 round trips a symbol), longer than the minute it
is scheduled for: every other run was skipped and the feed aged towards the
point where every symbol fails readiness. These tests pin the number of
statements a run sends and the fall-backs that keep a bad symbol or a failed
batch from losing the minute.
"""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.models import IntradayBar, IntradayNoTradeInterval
import app.models.stock_paper  # noqa: F401
from app.services import intraday_data

UTC = timezone.utc
MINUTE = timedelta(minutes=1)
FRIDAY_OPEN = datetime(2026, 9, 11, 13, 30, tzinfo=UTC)
THURSDAY_OPEN = datetime(2026, 9, 10, 13, 30, tzinfo=UTC)
WARM = datetime(2026, 9, 11, 15, 0, 30, tzinfo=UTC)
MEASURED = datetime(2026, 9, 11, 15, 2, 30, tzinfo=UTC)


def bar(opened_at: datetime) -> dict:
    return {"t": opened_at.isoformat().replace("+00:00", "Z"), "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 300}


def window(symbol, start, end):
    """Every minute traded except the thin names' minute 10 of each quarter hour (an IEX no-trade minute)."""
    rows, cursor = [], start
    while cursor < end:
        if not (symbol.startswith("T") and cursor.minute % 15 == 10):
            rows.append(bar(cursor))
        cursor += MINUTE
    return rows


def multi(symbols, start, end):
    return {symbol: (window(symbol, start, end), 0, False) for symbol in symbols}


def single(symbol, start, end):
    return window(symbol, start, end), 0, False


@pytest.fixture()
def universe():
    def make(count: int):
        symbols = [f"L{i:02d}" for i in range(count - count // 3)] + [f"T{i:02d}" for i in range(count // 3)]
        return symbols
    return make


@pytest.fixture()
def iex(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with ExitStack() as stack:
        for target, name, value in ((settings, "active_market_data_provider", "alpaca_iex"),
                                    (intraday_data, "MARKET_DATA_PROVIDER", "alpaca_iex"),
                                    (intraday_data, "MARKET_DATA_FEED_CLASS", "iex"),
                                    (settings, "paper_hedge_symbols", [])):
            stack.enter_context(patch.object(target, name, value))
        stack.enter_context(patch.object(intraday_data, "_fetch_bars_multi", side_effect=multi))
        stack.enter_context(patch.object(intraday_data, "_fetch_bars", side_effect=single))
        yield engine
    engine.dispose()


def _warm(engine, symbols):
    """Thursday complete and Friday imported through the warm-up: the steady state of a live session."""
    thursday_close = datetime(2026, 9, 10, 21, 0, tzinfo=UTC)
    with Session(engine) as db:
        for symbol in symbols:
            intraday_data.upsert_intraday_bars(db, symbol, window(symbol, THURSDAY_OPEN, THURSDAY_OPEN + 390 * MINUTE),
                                               ingested_at=thursday_close, verify_entitlement=True)
            for hour in range(7):  # what Thursday's own runs recorded: its no-trade minutes
                start = THURSDAY_OPEN + hour * 60 * MINUTE
                intraday_data.record_verified_no_trade_minutes(
                    db, symbol, start, start + 60 * MINUTE, observed_at=thursday_close,
                    provider="alpaca_iex", feed_class="iex")
    for step in range(3):
        with Session(engine) as db:
            intraday_data.ingest_intraday(db, symbols, now=WARM + timedelta(seconds=step))


def _statements(engine, symbols, now=MEASURED):
    count = {"statements": 0, "commits": 0}

    def statement(*_args):
        count["statements"] += 1

    def commit(*_args):
        count["commits"] += 1

    event.listen(engine, "before_cursor_execute", statement)
    event.listen(engine, "commit", commit)
    try:
        with Session(engine) as db:
            result = intraday_data.ingest_intraday(db, symbols, now=now)
    finally:
        event.remove(engine, "before_cursor_execute", statement)
        event.remove(engine, "commit", commit)
    return count, result


def test_a_steady_run_sends_the_same_statements_for_6_symbols_as_for_48(iex, monkeypatch, universe):
    counts = {}
    for size in (6, 48):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        symbols = universe(size)
        monkeypatch.setattr(settings, "paper_execution_symbols", symbols)
        _warm(engine, symbols)
        counts[size], result = _statements(engine, symbols)
        assert all(row["status"] == "complete" for row in result["results"]), result["results"][:2]
        assert all(row["exchange_timestamp"] and row["latency_seconds"] is not None for row in result["results"])
        engine.dispose()
    # The per-symbol path sent about 10 statements and 2 commits a symbol: 480+ statements for 48 symbols.
    assert counts[48]["statements"] <= 15 and counts[48]["commits"] <= 2, counts
    assert counts[48]["statements"] - counts[6]["statements"] <= 2, counts


def test_the_batch_stores_bars_no_trade_minutes_and_fresh_verification_for_every_symbol(iex, monkeypatch, universe):
    symbols = universe(9)
    monkeypatch.setattr(settings, "paper_execution_symbols", symbols)
    _warm(iex, symbols)
    _, result = _statements(iex, symbols)
    newest = MEASURED.replace(second=0) - 2 * MINUTE  # the last minute completed by 15:02:30
    with Session(iex) as db:
        for symbol in symbols:
            row = db.query(IntradayBar).filter_by(symbol=symbol, opened_at=newest).one()
            assert row.last_verified_at.replace(tzinfo=UTC) == MEASURED
        marked = {(row.symbol, row.opened_at.replace(tzinfo=UTC)) for row in db.query(IntradayNoTradeInterval).all()}
    # Each thin name's 14:40 and 14:55 minutes had no IEX print between verified bars: marked, not missing.
    assert {("T00", datetime(2026, 9, 11, 14, 40, tzinfo=UTC)), ("T00", datetime(2026, 9, 11, 14, 55, tzinfo=UTC))} <= marked
    assert not any(symbol.startswith("L") for symbol, _ in marked)
    by_symbol = {row["symbol"]: row for row in result["results"]}
    assert by_symbol["T00"]["status"] == "complete" and by_symbol["L00"]["rows_imported"] == 60


def test_a_symbol_whose_bars_cannot_be_parsed_is_reported_alone(iex, monkeypatch, universe):
    symbols = universe(6)
    monkeypatch.setattr(settings, "paper_execution_symbols", symbols)
    _warm(iex, symbols)

    def broken(symbols_, start, end):
        out = multi(symbols_, start, end)
        out["L01"] = ([{"t": "not a time", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}], 0, False)
        return out

    with patch.object(intraday_data, "_fetch_bars_multi", side_effect=broken):
        _, result = _statements(iex, symbols)
    by_symbol = {row["symbol"]: row for row in result["results"]}
    assert by_symbol["L01"]["status"] == "unavailable"
    assert all(row["status"] == "complete" for symbol, row in by_symbol.items() if symbol != "L01")


def test_a_failed_batch_falls_back_to_storing_symbol_by_symbol(iex, monkeypatch, universe, capsys):
    symbols = universe(6)
    monkeypatch.setattr(settings, "paper_execution_symbols", symbols)
    _warm(iex, symbols)
    real = intraday_data._store_bars
    calls = {"n": 0}

    def fail_once(db, bars_by_symbol, **kwargs):
        calls["n"] += 1
        if len(bars_by_symbol) > 1:
            raise RuntimeError("batch write failed")
        return real(db, bars_by_symbol, **kwargs)

    with patch.object(intraday_data, "_store_bars", side_effect=fail_once):
        _, result = _statements(iex, symbols)
    assert "batched store failed (RuntimeError); storing symbol by symbol" in capsys.readouterr().out
    assert calls["n"] == 1 + len(symbols)  # the batch, then each symbol on its own
    assert all(row["status"] == "complete" for row in result["results"])
    with Session(iex) as db:
        newest = MEASURED.replace(second=0) - 2 * MINUTE
        assert db.query(IntradayBar).filter_by(opened_at=newest).count() == len(symbols)


def test_the_latest_bar_query_looks_further_back_only_for_a_symbol_it_could_not_answer(iex, monkeypatch):
    monkeypatch.setattr(settings, "paper_execution_symbols", ["L00", "L01"])
    with Session(iex) as db:
        intraday_data.upsert_intraday_bars(db, "L00", [bar(datetime(2026, 9, 1, 15, 0, tzinfo=UTC))],
                                           ingested_at=datetime(2026, 9, 1, 21, 0, tzinfo=UTC))
        intraday_data.upsert_intraday_bars(db, "L01", [bar(datetime(2026, 9, 11, 15, 0, tzinfo=UTC))],
                                           ingested_at=datetime(2026, 9, 11, 16, 0, tzinfo=UTC))
        latest = intraday_data._latest_bars(db, ["L00", "L01", "L02"], THURSDAY_OPEN)
    assert latest["L00"].opened_at.replace(tzinfo=UTC) == datetime(2026, 9, 1, 15, 0, tzinfo=UTC)
    assert latest["L01"].opened_at.replace(tzinfo=UTC) == datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
    assert latest["L02"] is None


@pytest.mark.postgres
def test_overlapping_batched_imports_and_a_single_collector_never_deadlock(postgres_schema, monkeypatch):
    """Batch writers lock their symbols in one sorted statement, so overlapping sets queue instead of crossing."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import func, select

    from tests.postgres_support import WAIT_SECONDS, result_or_fail

    engine = postgres_schema.engine
    everyone = [f"L{i:02d}" for i in range(10)] + [f"T{i:02d}" for i in range(4)]
    for target, name, value in ((settings, "active_market_data_provider", "alpaca_iex"),
                                (intraday_data, "MARKET_DATA_PROVIDER", "alpaca_iex"),
                                (intraday_data, "MARKET_DATA_FEED_CLASS", "iex"),
                                (settings, "paper_hedge_symbols", []),
                                (settings, "paper_execution_symbols", everyone)):
        monkeypatch.setattr(target, name, value)
    monkeypatch.setattr(intraday_data, "_fetch_bars_multi", multi)
    monkeypatch.setattr(intraday_data, "_fetch_bars", single)
    sets = [everyone[:9], everyone[5:], list(reversed(everyone))]  # overlapping, in different orders

    for round_ in range(4):
        start = Barrier(len(sets) + 1, timeout=WAIT_SECONDS)
        now = WARM + timedelta(minutes=round_)

        def run_batch(symbols):
            start.wait()
            with Session(engine) as db:
                return len(intraday_data.ingest_intraday(db, symbols, now=now)["results"])

        def run_single():
            start.wait()
            with Session(engine) as db:
                return intraday_data.upsert_intraday_bars(
                    db, "L07", window("L07", now - 70 * MINUTE, now - 2 * MINUTE), ingested_at=now,
                    verify_entitlement=True)["rows_imported"]

        with ThreadPoolExecutor(max_workers=len(sets) + 1) as pool:
            batches = [pool.submit(run_batch, symbols) for symbols in sets]
            collector = pool.submit(run_single)
            assert [result_or_fail(future, "Batched import") for future in batches] == [len(s) for s in sets]
            assert result_or_fail(collector, "Single collector") > 0
    with Session(engine) as db:
        total = db.scalar(select(func.count()).select_from(IntradayBar))
        distinct = db.scalar(select(func.count()).select_from(
            select(IntradayBar.symbol, IntradayBar.opened_at).distinct().subquery()))
        assert total == distinct > 0
