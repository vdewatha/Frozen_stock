"""Expanded paper universe: config, assets, per-symbol gating, rate budget, sector cap."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.models.stock_paper  # noqa: F401  register paper tables
from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS, Settings, settings
from app.db.base import Base
from app.models import Asset, IntradayBar
from app.services import intraday_data, stock_monitoring
from app.services.seed import ensure_paper_execution_assets
from app.services.trading_universe import (
    SECTOR_GROUPS,
    classify_feed_outage,
    execution_symbols,
    intraday_symbols,
    sector_group,
)
from tests.test_stock_paper_autotrader import _ModelDb, _account, signal_source

UTC = timezone.utc


# --- configuration --------------------------------------------------------

def test_default_universe_is_liquid_and_fully_sector_mapped():
    assert 30 <= len(DEFAULT_PAPER_EXECUTION_SYMBOLS) <= 50
    assert len(set(DEFAULT_PAPER_EXECUTION_SYMBOLS)) == len(DEFAULT_PAPER_EXECUTION_SYMBOLS)
    assert set(DEFAULT_PAPER_EXECUTION_SYMBOLS) <= set(SECTOR_GROUPS)
    assert {"SPY", "QQQ", "AAPL", "MSFT"} <= set(DEFAULT_PAPER_EXECUTION_SYMBOLS)
    assert sector_group("NVDA") == "XLK" and sector_group("SPY") == "index"
    assert sector_group("ZZZZ") == "ZZZZ"  # unmapped names never pool with others


def test_settings_accept_the_expanded_list_and_bound_its_size():
    parsed = Settings(paper_execution_symbols=list(DEFAULT_PAPER_EXECUTION_SYMBOLS))
    assert len(parsed.paper_execution_symbols) == len(DEFAULT_PAPER_EXECUTION_SYMBOLS)
    with pytest.raises(ValidationError):
        Settings(paper_execution_symbols=[f"S{index}" for index in range(61)])


def test_intraday_universe_is_execution_symbols_plus_frozen_research_core():
    with patch.object(settings, "paper_execution_symbols", ["NVDA", "XLE"]):
        assert intraday_symbols() == {"AAPL", "MSFT", "QQQ", "SPY", "NVDA", "XLE"}
        assert intraday_data._symbol("nvda") == "NVDA"
        with pytest.raises(ValueError):
            intraday_data._symbol("XLK")
    # Research modules keep the frozen four-symbol core.
    assert intraday_data.ALLOWED_SYMBOLS == {"AAPL", "MSFT", "QQQ", "SPY"}


def test_execution_assets_are_created_once_and_deactivations_respected():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db, patch.object(settings, "paper_execution_symbols", ["SPY", "NVDA", "XLE"]):
            db.add(Asset(symbol="XLE", is_active=False))
            db.commit()
            assert sorted(ensure_paper_execution_assets(db)) == ["NVDA", "SPY"]
            db.commit()
            assert ensure_paper_execution_assets(db) == []
            nvda = db.query(Asset).filter_by(symbol="NVDA").one()
            assert nvda.is_active and nvda.sector == "Technology" and nvda.asset_type == "stock"
            assert db.query(Asset).filter_by(symbol="XLE").one().is_active is False
    finally:
        engine.dispose()


# --- per-symbol vs broad outage ---------------------------------------------

UNIVERSE = [f"S{index:02d}" for index in range(18)] + ["SPY", "QQQ"]
OPENED = datetime(2026, 10, 9, 14, 58, tzinfo=UTC)


def _ingested(symbol):
    """One persisted bar: the symbol has been ingested before (not onboarding)."""
    return IntradayBar(
        symbol=symbol, timeframe="1m", opened_at=OPENED, open=1, high=1, low=1, close=1, volume=1,
        provider=intraday_data.MARKET_DATA_PROVIDER, feed_class=intraday_data.MARKET_DATA_FEED_CLASS,
        exchange_timestamp=OPENED, ingested_at=OPENED,
    )


def test_never_ingested_symbols_are_blocked_but_not_counted_as_outage():
    failed = [f"S{index:02d}" for index in range(10)]
    result = classify_feed_outage(failed, UNIVERSE, onboarding=failed)
    assert result["scope"] == "partial"
    assert result["blocked_symbols"] == failed
    assert result["onboarding_symbols"] == failed
    # Every symbol onboarding (fresh database) stays fail-closed.
    assert classify_feed_outage(UNIVERSE, UNIVERSE, onboarding=UNIVERSE)["scope"] == "broad"


def test_isolated_stale_symbols_are_partial():
    result = classify_feed_outage(["S01", "S02"], UNIVERSE)
    assert result["scope"] == "partial"
    assert result["blocked_symbols"] == ["S01", "S02"]


def test_more_than_threshold_or_index_sentinel_is_broad():
    assert classify_feed_outage([f"S{index:02d}" for index in range(5)], UNIVERSE)["scope"] == "broad"
    assert classify_feed_outage([f"S{index:02d}" for index in range(4)], UNIVERSE)["scope"] == "partial"
    sentinel = classify_feed_outage(["QQQ"], UNIVERSE)
    assert sentinel["scope"] == "broad" and sentinel["index_sentinels_failed"] == ["QQQ"]
    assert classify_feed_outage([], [])["scope"] == "broad"
    assert classify_feed_outage([], UNIVERSE)["scope"] == "none"


def _monitor(failed, symbols=UNIVERSE, ingested=None):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    ready = {
        "status": "ready", "execution_status": "ready", "entitlement_state": "verified",
        "exchange_timestamp": datetime(2026, 10, 9, 14, 59, tzinfo=UTC),
    }
    stale = {**ready, "status": "stale", "execution_status": "stale"}
    try:
        with Session(engine) as db:
            db.add_all([Asset(symbol=symbol, asset_type="stock", is_active=True) for symbol in symbols])
            db.add_all([_ingested(symbol) for symbol in (symbols if ingested is None else ingested)])
            db.commit()
            with patch.object(settings, "paper_execution_symbols", list(symbols)), \
                 patch.object(stock_monitoring, "feed_status",
                              side_effect=lambda _db, symbol: stale if symbol in failed else ready):
                return stock_monitoring._freshness_and_provenance(db)
    finally:
        engine.dispose()


def test_monitor_one_stale_symbol_is_warning_not_a_pause():
    result = _monitor({"S03"})
    assert result["status"] == "warning"
    assert result["value"]["failed_symbols"] == ["S03"]
    assert result["details"]["outage_scope"] == "partial"


def test_monitor_broad_or_sentinel_outage_is_still_a_safety_breach():
    assert _monitor({f"S{index:02d}" for index in range(5)})["status"] == "breach"
    assert _monitor({"SPY"})["status"] == "breach"
    # Small universes keep the old fail-closed behaviour: one of four is 25%.
    assert _monitor({"AAPL"}, ["AAPL", "MSFT", "QQQ", "SPY"])["status"] == "breach"


def test_monitor_newly_added_symbols_do_not_pause_the_stock_path():
    new = {f"S{index:02d}" for index in range(12)}  # 60% of the universe, never ingested
    result = _monitor(new, ingested=[symbol for symbol in UNIVERSE if symbol not in new])
    assert result["status"] == "warning"
    assert result["details"]["onboarding_symbols"] == sorted(new)


def test_readiness_blocks_only_on_broad_outage(monkeypatch):
    from app.services import readiness

    symbols = ["AAPL", "MSFT", "NVDA", "AMD", "XLE", "XLF", "SPY", "QQQ", "JPM", "KO"]
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add_all([Asset(symbol=symbol, asset_type="stock", is_active=True) for symbol in symbols])
    db.add_all([_ingested(symbol) for symbol in symbols])
    db.commit()
    failed: set[str] = set()

    def validate(_db, symbol, **_kwargs):
        if symbol in failed:
            raise readiness.UntrustedMarketData("Latest completed bar is stale; paper decisions are blocked.")
        return {"status": "ready"}

    monkeypatch.setattr(settings, "paper_execution_symbols", symbols)
    monkeypatch.setattr(readiness, "trusted_history", lambda *_a, **_k: (None, "yfinance"))
    monkeypatch.setattr(readiness, "validate_intraday_readiness", validate)
    monkeypatch.setattr(readiness, "feed_status", lambda _db, symbol: {"status": "stale"})
    try:
        def intraday_check():
            snapshot = readiness_checks(readiness, db)
            return next(check for check in snapshot if check["name"] == "Intraday feed")

        assert intraday_check()["status"] == "ready"
        failed.update({"KO"})
        check = intraday_check()
        assert check["status"] == "warning"
        assert check["details"]["blocked_symbols"] == ["KO"]
        failed.update({"JPM", "XLF"})  # 30% > 20%
        assert intraday_check()["status"] == "blocked"
        failed.clear()
        failed.add("SPY")
        assert intraday_check()["status"] == "blocked"
    finally:
        db.close()
        engine.dispose()


def readiness_checks(readiness, db):
    """Run readiness_snapshot with the non-market-data dependencies stubbed."""
    broker = {
        "paper_trading_enabled": True, "live_trading_blocked": True, "message": "ok",
        "accounting": {"ready": True, "reason": "ok"}, "paper_execution": {"ready": True},
    }
    with patch.object(readiness, "stock_paper_broker_status", return_value=broker), \
         patch.object(readiness, "portfolio_risk_snapshot", return_value={"alerts": []}), \
         patch.object(readiness, "_scheduler_health", return_value={"healthy": True}), \
         patch("app.services.stock_recovery.recovery_status", return_value={"status": "not_required"}), \
         patch("app.services.live_safety.evaluate_live_safety", return_value={}):
        return readiness.readiness_snapshot(db)["checks"]


# --- intraday rate-limit design ---------------------------------------------

def _bars(start, end):
    minutes = int((end - start).total_seconds() // 60)
    return [{
        "t": (start + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
        "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000,
    } for i in range(minutes)]


def test_latest_window_uses_one_multi_symbol_request_for_the_universe():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    observed = datetime(2026, 9, 11, 14, 5, tzinfo=UTC)  # 10:05 ET; bars complete through 10:04
    symbols = ["AAPL", "MSFT", "NVDA", "XLE", "SPY"]
    multi_calls, single_calls = [], []

    def multi(chunk, start, end):
        multi_calls.append(list(chunk))
        return {symbol: _bars(start, end) for symbol in chunk}

    def single(symbol, start, end):
        single_calls.append((symbol, start, end))
        return [], 0, False

    try:
        with patch.object(settings, "paper_execution_symbols", symbols), \
             patch.object(settings, "active_market_data_provider", "alpaca_iex"), \
             patch.object(settings, "intraday_backfill_request_budget", 0), \
             patch("app.services.alpaca_research_data.fetch_iex_bars", side_effect=multi), \
             patch.object(intraday_data, "_fetch_bars", side_effect=single):
            result = intraday_data.ingest_intraday(db, symbols, now=observed, repair_sessions=1)
        assert multi_calls == [symbols]
        # Latest window came from the batch; the zero backfill budget stops
        # every per-symbol repair request.
        assert single_calls == []
        assert result["multi_symbol_prefetch"] == 5
        assert [row["symbol"] for row in result["results"]] == symbols
        assert all(row["rows_imported"] == 34 for row in result["results"])
    finally:
        db.close()
        engine.dispose()


def test_backfill_requests_share_one_bounded_budget():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    observed = datetime(2026, 9, 11, 18, 0, tzinfo=UTC)
    symbols = ["AAPL", "MSFT", "NVDA", "XLE", "SPY"]
    calls = []

    def single(symbol, start, end):
        calls.append(symbol)
        return [], 0, False

    try:
        with patch.object(settings, "paper_execution_symbols", symbols), \
             patch.object(settings, "active_market_data_provider", "tradier"), \
             patch.object(settings, "intraday_backfill_chunks_per_cycle", 8), \
             patch.object(settings, "intraday_backfill_request_budget", 3), \
             patch.object(intraday_data, "_fetch_bars", side_effect=single):
            result = intraday_data.ingest_intraday(db, symbols, now=observed)
        # One newest-window request per symbol (Tradier has no batch
        # endpoint) plus at most three shared repair requests.
        assert len(calls) == len(symbols) + 3
        assert result["backfill_requests_remaining"] == 0
        assert all(row["status"] == "incomplete" for row in result["results"])
    finally:
        db.close()
        engine.dispose()


# --- sector / correlation cap ------------------------------------------------

def _cycle(db, symbols, actions):
    order = SimpleNamespace(id=42, status="accepted")

    def signal(_db, symbol, slug):
        return {"signal_id": 1, "signal_action": actions.get(symbol, "HOLD"), "reference_price": "100"}

    with patch.object(settings, "paper_execution_symbols", symbols), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=_account()), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         signal_source(False, side_effect=signal), \
         patch("app.services.stock_paper_autotrader._strategy_holdings", return_value=({}, True)), \
         patch("app.services.stock_paper_autotrader._pending_orders", return_value=[]), \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order", return_value=order) as reserve, \
         patch("app.services.stock_paper_autotrader.dispatch_reserved_order", return_value=order):
        from app.services.stock_paper_autotrader import run_stock_paper_signal_cycle
        result = run_stock_paper_signal_cycle(db)
    return result, reserve


_MA = SimpleNamespace(id=1, strategy_type="moving_average_crossover")


def test_sector_cap_blocks_a_fourth_name_in_the_same_sector_group():
    held = [SimpleNamespace(symbol=symbol, quantity=Decimal("1")) for symbol in ("AAPL", "MSFT", "NVDA")]
    db = _ModelDb([_MA], {"StockPaperPosition": held})
    result, reserve = _cycle(db, ["AMD", "XLE"], {"AMD": "BUY", "XLE": "BUY"})
    by_symbol = {row["symbol"]: row for row in result["results"] if row["strategy"] == "moving_average_crossover"}
    assert by_symbol["AMD"]["status"] == "skipped"
    assert "sector group XLK" in by_symbol["AMD"]["reason"]
    assert by_symbol["XLE"]["status"] == "submitted"
    assert [call.kwargs["symbol"] for call in reserve.call_args_list] == ["XLE"]


def test_sector_cap_counts_same_cycle_entries_and_can_be_disabled():
    db = _ModelDb([_MA], {})
    symbols = ["AAPL", "AMD", "MSFT", "NVDA"]
    result, reserve = _cycle(db, symbols, {symbol: "BUY" for symbol in symbols})
    assert reserve.call_count == 3
    skipped = [row["symbol"] for row in result["results"] if row.get("status") == "skipped"]
    assert skipped == ["NVDA"]
    with patch.object(settings, "paper_max_positions_per_sector", 0):
        _result, reserve = _cycle(_ModelDb([_MA], {}), symbols, {symbol: "BUY" for symbol in symbols})
    assert reserve.call_count == 4
