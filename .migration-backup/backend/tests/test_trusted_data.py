from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import unittest

from app.services import paper_trading, model_tracking, portfolio_risk
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


def test_mixed_provenance_requires_normalization():
    frame = history()
    frame.loc[0, "source"] = "yahoo_chart"
    with unittest.TestCase().assertRaises(UntrustedMarketData):
        validate_history(frame)


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
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [SimpleNamespace(symbol="SPY")]
    with patch.object(trade_candidates, "trusted_history", side_effect=UntrustedMarketData("missing")), patch.object(trade_candidates, "summarize_macro_context", return_value={}), patch.object(trade_candidates, "latest_market_regime", return_value=None):
        result = trade_candidates.scan_trade_candidates(db)
    assert result["candidates"] == []
    assert result["blocked_assets"] == [{"symbol": "SPY", "reason": "missing"}]


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(unittest.FunctionTestCase(value) for name, value in globals().items() if name.startswith("test_"))
