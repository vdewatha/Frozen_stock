from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import unittest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services import market_data, paper_trading, model_tracking, portfolio_risk
from app.db.base import Base
from app.models import MarketPrice
from app.services.trusted_data import UntrustedMarketData, trusted_history, validate_history


def history():
    return pd.DataFrame({"date": pd.date_range(end=pd.Timestamp.now(tz="UTC").normalize(), periods=150),
                         "open": 100., "high": 102., "low": 98., "close": 101., "volume": 10,
                         "source": "yfinance"})


def invalid_history_is_rejected(bad):
    frame = history()
    if bad == "synthetic": frame.loc[0, "source"] = "synthetic_fallback"
    if bad == "nan": frame.loc[0, "close"] = float("nan")
    if bad == "stale": frame["date"] -= pd.Timedelta(days=10)
    if bad == "duplicate": frame.loc[1, "date"] = frame.loc[0, "date"]
    if bad == "future": frame.loc[149, "date"] += pd.Timedelta(days=1)
    if bad == "bounds": frame.loc[0, "high"] = 50
    if bad == "short": frame = frame.iloc[-10:]
    with unittest.TestCase().assertRaises(UntrustedMarketData): validate_history(frame)


def test_invalid_history_is_rejected():
    for bad in ["synthetic", "nan", "stale", "duplicate", "future", "bounds", "short"]:
        invalid_history_is_rejected(bad)


def test_trusted_read_never_fetches_or_seeds():
    db = MagicMock()
    with patch("app.services.trusted_data.get_price_history", return_value=(history(), "database:yfinance")) as read:
        trusted_history(db, "SPY")
        read.assert_called_once_with(db, "SPY", 260, auto_seed=False)


def test_trusted_history_applies_adjusted_close_factor_to_ohlc():
    frame = history()
    frame["adjusted_close"] = frame["close"] * 0.5
    with patch("app.services.trusted_data.get_price_history", return_value=(frame, "database:yfinance")):
        normalized, _ = trusted_history(MagicMock(), "SPY")
    assert normalized.iloc[-1]["close"] == frame.iloc[-1]["adjusted_close"]
    assert normalized.iloc[-1]["open"] == frame.iloc[-1]["open"] * 0.5


def test_inactive_symbol_rejected_before_read():
    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = None
    with patch("app.services.trusted_data.get_price_history") as read, unittest.TestCase().assertRaises(UntrustedMarketData):
        trusted_history(db, "XYZ")
    read.assert_not_called()


def test_readiness_warning_blocks_signal_before_model_or_order():
    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = SimpleNamespace(id=1)
    with patch.object(paper_trading, "readiness_snapshot", return_value={"paper_trading_allowed": False, "checks": [{"name": "Model freshness", "status": "warning"}]}), patch.object(paper_trading, "write_audit_log"), patch.object(paper_trading, "trusted_history") as read, patch.object(paper_trading, "submit_paper_order") as order:
        result = paper_trading.run_paper_signal(db, "SPY", "test")
    assert result["action"] == "BLOCKED"
    read.assert_not_called()
    order.assert_not_called()


def test_model_missing_history_persists_nothing():
    db = MagicMock()
    with patch.object(model_tracking, "trusted_history", side_effect=UntrustedMarketData("missing")), unittest.TestCase().assertRaises(UntrustedMarketData):
        model_tracking.run_and_persist_model_predictions(db, "SPY")
    db.add.assert_not_called()


def test_valuation_unavailable_explicitly_marked():
    with patch.object(portfolio_risk, "trusted_history", side_effect=UntrustedMarketData("missing")):
        assert portfolio_risk._latest_price(MagicMock(), "SPY", 100) == (100, "valuation_unavailable")


def test_realization_uses_bars_not_calendar_days():
    frame = pd.DataFrame({"date": ["2026-08-28", "2026-08-31", "2026-09-01"], "close": [100, 110, 120]})
    assert model_tracking.realization_prices(frame, date(2026, 8, 28), 2) == (100, 120)
    assert model_tracking.realization_prices(frame.iloc[:2], date(2026, 8, 28), 2) == (None, None)


def test_synthetic_regime_cannot_inform_execution():
    db = MagicMock()
    with patch.object(paper_trading, "latest_market_regime", return_value={"features": {"source": "synthetic_fallback"}}) as regime:
        assert paper_trading._trusted_regime(db) is None
        regime.assert_called_once_with(db, auto_detect=False)


def test_legacy_macro_derived_regime_cannot_inform_execution():
    db = MagicMock()
    legacy = {"features": {"source": "yfinance", "macro_context": {"rate_regime": "restrictive"}}}
    with patch.object(paper_trading, "latest_market_regime", return_value=legacy):
        assert paper_trading._trusted_regime(db) is None


def test_mixed_provenance_requires_normalization():
    frame = history()
    frame.loc[0, "source"] = "yahoo_chart"
    with unittest.TestCase().assertRaises(UntrustedMarketData):
        validate_history(frame)


def test_market_import_reports_trusted_fallback_without_synthetic_data():
    db = MagicMock()
    fallback = history().iloc[:80].copy()
    with patch.object(market_data, "fetch_yfinance_prices", return_value=pd.DataFrame()), \
         patch.object(market_data, "fetch_yahoo_chart_prices", return_value=fallback), \
         patch.object(market_data, "upsert_prices", return_value=80):
        result = market_data.import_market_prices(db, "SPY")
    assert result["source"] == "yahoo_chart"
    assert result["trusted"] is True
    assert result["synthetic_fallback_used"] is False
    assert [attempt["provider"] for attempt in result["provider_attempts"]] == ["yfinance", "yahoo_chart"]
    assert result["provider_attempts"][0]["status"] == "unavailable"
    assert result["provider_attempts"][1]["status"] == "ready"


def test_market_import_is_explicitly_unavailable_when_trusted_sources_fail():
    db = MagicMock()
    with patch.object(market_data, "fetch_yfinance_prices", return_value=pd.DataFrame()), \
         patch.object(market_data, "fetch_yahoo_chart_prices", return_value=pd.DataFrame()), \
         patch.object(type(market_data.settings), "research_alpaca_credentials", return_value=("", "")), \
         patch.object(market_data, "upsert_prices", return_value=0):
        result = market_data.import_market_prices(db, "SPY")
    assert result["source"] == "unavailable"
    assert result["trusted"] is False
    assert result["synthetic_fallback_used"] is False
    assert result["unavailable_reason"]
    assert all(attempt["status"] == "unavailable" for attempt in result["provider_attempts"])


def test_market_import_does_not_replace_a_date_with_a_different_provider():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    prices = pd.DataFrame([{
        "date": date(2026, 10, 6), "open": 100, "high": 102,
        "low": 99, "close": 101, "adjusted_close": 101, "volume": 100,
    }])
    with Session(engine) as db:
        market_data.upsert_prices(db, "AAPL", prices, "yfinance")
        replacement = prices.copy()
        replacement.loc[0, "close"] = 999
        market_data.upsert_prices(db, "AAPL", replacement, "yahoo_chart")
        row = db.query(MarketPrice).filter_by(symbol="AAPL").one()
        assert row.source == "yfinance"
        assert row.close == 101

        replacement.loc[0, "close"] = 102
        market_data.upsert_prices(db, "AAPL", replacement, "yfinance")
        row = db.query(MarketPrice).filter_by(symbol="AAPL").one()
        assert row.source == "yfinance"
        assert row.close == 102


def test_research_persistence_rejects_missing_prices():
    from app.services import candidate_evidence, experiments, market_regime
    for module, invoke in [
        (candidate_evidence, lambda db: candidate_evidence.candidate_evidence_drilldown(db, "SPY", "test")),
        (experiments, lambda db: experiments.run_strategy_experiments(db, symbol="SPY", strategy_slug="test")),
        (market_regime, lambda db: market_regime.detect_and_store_market_regime(db)),
    ]:
        db = MagicMock()
        with patch.object(module, "trusted_history", side_effect=UntrustedMarketData("missing")), unittest.TestCase().assertRaises(UntrustedMarketData):
            invoke(db)
        db.add.assert_not_called()


def test_scanner_reports_missing_data_without_candidate():
    from app.services import trade_candidates
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [("SPY",)]
    with patch.object(trade_candidates, "trusted_history", side_effect=UntrustedMarketData("missing")), patch.object(trade_candidates, "summarize_macro_context", return_value={}), patch.object(trade_candidates, "latest_market_regime", return_value=None):
        result = trade_candidates.scan_trade_candidates(db)
    assert result["candidates"] == []
    assert result["blocked_assets"] == [{"symbol": "SPY", "reason": "missing"}]


def test_scanner_uses_scalar_symbol_for_news_context():
    from app.services import trade_candidates

    db = MagicMock()
    asset_query = MagicMock()
    asset_query.filter.return_value.order_by.return_value.all.return_value = [("SPY",)]
    strategy_query = MagicMock()
    strategy_query.order_by.return_value.all.return_value = []
    db.query.side_effect = [asset_query, strategy_query]
    with patch.object(trade_candidates, "trusted_history", return_value=(history(), "database:yfinance")), \
         patch.object(trade_candidates, "summarize_macro_context", return_value={"summary": ""}), \
         patch.object(trade_candidates, "latest_market_regime", return_value=None), \
         patch.object(trade_candidates, "predict_probabilities", return_value={"predictions": []}), \
         patch.object(trade_candidates, "summarize_news_context", return_value={"summary": ""}) as summarize_news:
        result = trade_candidates.scan_trade_candidates(db)

    assert result["candidates"] == []
    summarize_news.assert_called_once_with(db, "SPY")


def test_scanner_marks_research_only_symbols_as_not_execution_eligible():
    from app.services import trade_candidates
    from types import SimpleNamespace

    db = MagicMock()
    asset_query = MagicMock()
    asset_query.filter.return_value.order_by.return_value.all.return_value = [("QQQ",)]
    strategy_query = MagicMock()
    strategy_query.order_by.return_value.all.return_value = [
        SimpleNamespace(
            id=1,
            name="Test strategy",
            strategy_type="test",
            parameters={},
            current_status="active",
        )
    ]
    db.query.side_effect = [asset_query, strategy_query]
    signal = MagicMock(action="BUY", confidence=0.8, probability_up=0.6, reason="test")
    with patch.object(trade_candidates.settings, "paper_execution_symbols", ["SPY"]), \
         patch.object(trade_candidates, "trusted_history", return_value=(history(), "database:yfinance")), \
         patch.object(trade_candidates, "summarize_macro_context", return_value={"summary": ""}), \
         patch.object(trade_candidates, "latest_market_regime", return_value=None), \
         patch.object(trade_candidates, "predict_probabilities", return_value={"predictions": [{"probability_up": 0.6, "expected_return": 0.01, "horizon_days": 1}]}), \
         patch.object(trade_candidates, "summarize_news_context", return_value={"summary": ""}), \
         patch.object(trade_candidates, "get_strategy") as get_strategy, \
         patch.object(trade_candidates, "run_backtest", return_value={"score": 0.2, "rejected": False, "rejection_reasons": []}):
        get_strategy.return_value.generate_signal.return_value = signal
        result = trade_candidates.scan_trade_candidates(db)

    candidate = result["candidates"][0]
    assert candidate["execution_eligible"] is False
    assert candidate["execution_blocker"] == "research_only_symbol"
    assert any("research-only" in blocker for blocker in candidate["blockers"])
    assert result["positive_count"] == 0
    assert result["research_positive_count"] == 1


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(unittest.FunctionTestCase(value) for name, value in globals().items() if name.startswith("test_"))
