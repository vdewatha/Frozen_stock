"""Symbols traded outside the execution list get minute bars on the scheduled in-session run.

2026-11-16: the minutely run fetched the configured execution list (and the
slow book's ETFs) only. A news-discovered symbol got bars from the
half-hourly self-healing repair instead: NOC, admitted 11:10 ET, was ingested
four times all session, each on the half hour, while SPY was ingested every
minute. The cycle buys only on a bar under two minutes old, so news-found
symbols were tradable for a few minutes in thirty.
"""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models.stock_paper  # noqa: F401  (registers paper tables)
from app.core.config import settings
from app.db.base import Base
from app.models import IntradayBar
from app.services import intraday_data, trading_universe

UTC = timezone.utc
NOW = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)  # a regular session
MINUTE = timedelta(minutes=1)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def ready(symbol):
    return {"symbol": symbol, "status": "ready", "exchange_timestamp": NOW - timedelta(minutes=2),
            "ingestion_timestamp": NOW, "latency_seconds": 60.0, "missing_intervals": [], "unavailable_reason": None}


def universe(stack, *, discovered=(), lane=(), hedge=(), book=False):
    """A two-symbol execution list plus the given off-list sources."""
    for item in (
        patch.object(settings, "paper_execution_symbols", ["SPY", "QQQ"]),
        patch.object(settings, "paper_hedge_symbols", list(hedge)),
        patch.object(settings, "target_weight_book_enabled", book),
        patch.object(trading_universe, "discovered_symbols", return_value=frozenset(discovered)),
        patch.object(trading_universe, "exploration_lane_symbols", return_value=frozenset(lane)),
    ):
        stack.enter_context(item)


def run(db, symbols=None, *, feed_only_status="complete", **sources):
    def ingest(db, selected, **kwargs):
        return {"status": "complete", "results": [
            {"symbol": s, "status": "complete" if s in {"SPY", "QQQ"} else feed_only_status, "rows_imported": 1}
            for s in selected]}

    with ExitStack() as stack:
        universe(stack, **sources)
        stack.enter_context(patch.object(
            settings, "tradier_market_data_api_key", type(settings.tradier_market_data_api_key)("key")))
        ingest_mock = stack.enter_context(patch.object(intraday_data, "ingest_intraday", side_effect=ingest))
        stack.enter_context(patch.object(
            intraday_data, "feed_status", side_effect=lambda db, symbol, now: ready(symbol)))
        result = intraday_data.preflight_intraday(db, symbols, now=NOW)
    return result, ingest_mock.call_args.args[1]


def test_scheduled_run_ingests_news_found_symbols_in_the_same_batch(db):
    result, ingested = run(db, discovered={"NOC", "SHEL"})
    assert ingested == ["SPY", "QQQ", "NOC", "SHEL"]
    # They are fetched, not judged: readiness is still the execution list.
    assert result["symbols"] == ["SPY", "QQQ"]
    assert {row["symbol"] for row in result["results"]} == {"SPY", "QQQ"}
    assert result["ready"] is True
    assert result["feed_only_symbols"] == [
        {"symbol": "NOC", "status": "complete", "rows_imported": 1},
        {"symbol": "SHEL", "status": "complete", "rows_imported": 1},
    ]


def test_lane_and_hedge_symbols_join_after_the_book(db):
    result, ingested = run(db, discovered={"NOC"}, lane={"PLTR"}, hedge=["SH"], book=True)
    assert ingested[:2] == ["SPY", "QQQ"]
    assert ingested[2:4] == ["SGOV", "VOO"]
    assert ingested[4:] == ["NOC", "PLTR", "SH"]
    assert [row["symbol"] for row in result["book_symbols"]] == ["SGOV", "VOO"]
    assert [row["symbol"] for row in result["feed_only_symbols"]] == ["NOC", "PLTR", "SH"]


def test_a_symbol_already_fetched_is_not_fetched_twice(db):
    _result, ingested = run(db, discovered={"SPY", "VOO", "NOC"}, book=True)
    assert ingested == ["SPY", "QQQ", "SGOV", "VOO", "NOC"]


def test_a_failing_news_symbol_never_blocks_readiness(db):
    result, _ = run(db, discovered={"NOC"}, feed_only_status="unavailable")
    assert result["ready"] is True and result["status"] == "ready"
    assert result["feed_only_symbols"] == [{"symbol": "NOC", "status": "unavailable", "rows_imported": 1}]


def test_explicit_symbol_lists_are_left_alone(db):
    result, ingested = run(db, symbols=["SPY"], discovered={"NOC"})
    assert ingested == ["SPY"]
    assert "feed_only_symbols" not in result


def test_nothing_off_the_list_adds_nothing(db):
    result, ingested = run(db)
    assert ingested == ["SPY", "QQQ"]
    assert "feed_only_symbols" not in result


def test_a_symbol_the_feed_refuses_leaves_the_other_sources():
    with ExitStack() as stack:
        universe(stack, lane={"PLTR"}, hedge=["SH"])
        assert intraday_data._feed_only_symbols(["SPY", "QQQ"], NOW) == ["PLTR", "SH"]
        # One source names a symbol the feed will not take: the other source is still fetched.
        stack.enter_context(patch.object(
            intraday_data, "_symbol", side_effect=lambda value: (_ for _ in ()).throw(ValueError(value))
            if value == "PLTR" else value))
        assert intraday_data._feed_only_symbols(["SPY", "QQQ"], NOW) == ["SH"]


def test_a_source_that_raises_is_skipped():
    with ExitStack() as stack:
        universe(stack, hedge=["SH"])
        stack.enter_context(patch.object(trading_universe, "exploration_lane_symbols", side_effect=RuntimeError("down")))
        # intraday_symbols() itself reads the lane, so the hedge symbol is checked against a stubbed universe.
        stack.enter_context(patch.object(intraday_data, "_symbol", side_effect=str))
        assert intraday_data._feed_only_symbols(["SPY", "QQQ"], NOW) == ["SH"]


def test_many_off_list_symbols_take_turns_and_all_get_a_turn():
    lane = {f"L{index:02d}" for index in range(40)}
    cap = intraday_data.FEED_ONLY_SYMBOLS_PER_RUN
    with ExitStack() as stack:
        universe(stack, lane=lane)
        seen: set[str] = set()
        for minute in range(8):
            chosen = intraday_data._feed_only_symbols(["SPY", "QQQ"], NOW + MINUTE * minute)
            assert len(chosen) == cap and len(set(chosen)) == cap
            assert set(chosen) <= lane
            seen.update(chosen)
        assert seen == lane
        # The same minute always picks the same window.
        assert (intraday_data._feed_only_symbols(["SPY", "QQQ"], NOW)
                == intraday_data._feed_only_symbols(["SPY", "QQQ"], NOW + timedelta(seconds=40)))


def test_at_the_cap_every_symbol_is_fetched_every_minute():
    lane = {f"L{index:02d}" for index in range(intraday_data.FEED_ONLY_SYMBOLS_PER_RUN)}
    with ExitStack() as stack:
        universe(stack, lane=lane)
        for minute in range(3):
            assert intraday_data._feed_only_symbols(["SPY", "QQQ"], NOW + MINUTE * minute) == sorted(lane)


def test_the_scheduled_in_session_job_stores_a_news_found_symbols_newest_bars(db):
    """End to end through ``collect_scheduled_intraday``, with only the provider faked."""
    def bar(opened_at):
        return {"t": opened_at.isoformat().replace("+00:00", "Z"), "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 300}

    def fetch_multi(symbols, start, end):
        minutes = int((end - start).total_seconds() // 60)
        return {symbol: ([bar(start + MINUTE * index) for index in range(minutes)], 0, False) for symbol in symbols}

    with ExitStack() as stack:
        universe(stack, discovered={"NOC"})
        for target, name, value in (
            (settings, "active_market_data_provider", "alpaca_iex"),
            (settings, "active_paper_broker", "alpaca_paper"),
            (settings, "allow_live_trading", False),
            (intraday_data, "MARKET_DATA_PROVIDER", "alpaca_iex"),
            (intraday_data, "MARKET_DATA_FEED_CLASS", "iex"),
            (settings, "paper_alpaca_api_key", type(settings.paper_alpaca_api_key)("key")),
            (settings, "paper_alpaca_api_secret", type(settings.paper_alpaca_api_secret)("secret")),
        ):
            stack.enter_context(patch.object(target, name, value))
        fetched = stack.enter_context(patch.object(intraday_data, "_fetch_bars_multi", side_effect=fetch_multi))
        stack.enter_context(patch.object(intraday_data, "_fetch_bars", return_value=([], 0, False)))
        result = intraday_data.collect_scheduled_intraday(db, now=NOW)

    assert fetched.call_args.args[0] == ["SPY", "QQQ", "NOC"]
    assert [row["symbol"] for row in result["feed_only_symbols"]] == ["NOC"]
    newest = {
        symbol: db.query(IntradayBar).filter(IntradayBar.symbol == symbol)
        .order_by(IntradayBar.opened_at.desc()).first()
        for symbol in ("SPY", "NOC")
    }
    assert newest["NOC"] is not None
    # The news-found symbol is as fresh as the execution list, from the same run.
    assert newest["NOC"].opened_at == newest["SPY"].opened_at
    assert newest["NOC"].ingested_at == newest["SPY"].ingested_at
    assert {row["symbol"] for row in result["results"]} == {"SPY", "QQQ"}
