"""The slow book's ETFs are ingested on the scheduled in-session run, outside readiness.

2026-11-09: tranche 1 was blocked "no fresh reference price for SGOV, VOO"
because the in-session preflight ingested only the execution symbols.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models.stock_paper  # noqa: F401  (registers paper tables)
from app.core.config import settings
from app.db.base import Base
from app.services import intraday_data

UTC = timezone.utc
NOW = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)  # a regular session


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def ready(symbol):
    return {"symbol": symbol, "status": "ready", "exchange_timestamp": NOW - timedelta(minutes=2),
            "ingestion_timestamp": NOW, "latency_seconds": 60.0, "missing_intervals": [], "unavailable_reason": None}


def run(db, symbols=None, book_status="complete"):
    def ingest(db, selected, **kwargs):
        return {"status": "complete", "results": [
            {"symbol": s, "status": book_status if s in {"VOO", "SGOV"} else "complete", "rows_imported": 1}
            for s in selected]}

    with patch.object(settings, "tradier_market_data_api_key", type(settings.tradier_market_data_api_key)("key")), \
            patch.object(settings, "paper_execution_symbols", ["SPY", "QQQ"]), \
            patch.object(settings, "target_weight_book_enabled", True), \
            patch.object(intraday_data, "ingest_intraday", side_effect=ingest) as ingest_mock, \
            patch.object(intraday_data, "feed_status", side_effect=lambda db, symbol, now: ready(symbol)):
        result = intraday_data.preflight_intraday(db, symbols, now=NOW)
    return result, ingest_mock.call_args.args[1]


def test_scheduled_run_ingests_book_symbols_in_the_same_batch(db):
    result, ingested = run(db)
    assert ingested == ["SPY", "QQQ", "SGOV", "VOO"]
    assert result["symbols"] == ["SPY", "QQQ"]
    assert {row["symbol"] for row in result["results"]} == {"SPY", "QQQ"}
    assert result["ready"] is True
    assert [row["symbol"] for row in result["book_symbols"]] == ["SGOV", "VOO"]


def test_book_symbol_failure_never_blocks_readiness(db):
    result, _ = run(db, book_status="unavailable")
    assert result["ready"] is True and result["status"] == "ready"
    assert {row["status"] for row in result["book_symbols"]} == {"unavailable"}


def test_explicit_symbol_lists_are_left_alone(db):
    result, ingested = run(db, symbols=["SPY"])
    assert ingested == ["SPY"]
    assert "book_symbols" not in result


def test_disabled_book_adds_nothing(db):
    with patch.object(settings, "target_weight_book_enabled", False):
        assert intraday_data._book_only_symbols(["SPY"]) == []
