from unittest.mock import patch
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import Asset
from app.services import stock_monitoring
from app.models import StockMonitoringBreach, StockPaperRecoveryState


def test_research_only_assets_do_not_block_execution_feed_gate():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            db.add_all([
                Asset(symbol="AAPL", asset_type="stock", is_active=True),
                Asset(symbol="AMD", asset_type="stock", is_active=True),
            ])
            db.commit()
            with patch.object(stock_monitoring.settings, "paper_execution_symbols", ["AAPL"]), \
                 patch.object(stock_monitoring, "feed_status", return_value={
                     "status": "ready",
                     "execution_status": "ready",
                     "entitlement_state": "verified",
                     "exchange_timestamp": datetime(2026, 10, 9, 14, 59, tzinfo=timezone.utc),
                 }):
                result = stock_monitoring._freshness_and_provenance(db)

            assert result["status"] == "clear"
            assert result["value"] == {"failed_symbols": [], "ready_symbols": ["AAPL"]}
            assert result["details"]["research_only_symbols"] == ["AMD"]
            assert result["details"]["research_only_excluded_from_execution_gate"] is True
    finally:
        engine.dispose()


def test_market_closed_historical_gaps_are_warning_not_safety_breach():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            db.add(Asset(symbol="SPY", asset_type="stock", is_active=True))
            db.commit()
            with patch.object(stock_monitoring.settings, "paper_execution_symbols", ["SPY"]), patch.object(
                stock_monitoring,
                "feed_status",
                return_value={
                    "status": "incomplete",
                    "execution_status": "market_closed",
                    "entitlement_state": "verified",
                    "exchange_timestamp": datetime(2026, 10, 8, 20, tzinfo=timezone.utc),
                    "provider": "alpaca_iex",
                    "feed_class": "iex",
                    "missing_intervals": ["2026-10-07T16:33:00+00:00"],
                },
            ):
                result = stock_monitoring._freshness_and_provenance(db)

            assert result["status"] == "warning"
            assert result["value"]["failed_symbols"] == []
            assert result["details"]["historical_warnings"] == {"SPY": "Historical repair gaps remain"}
            assert result["details"]["historical_warnings"]["SPY"] == "Historical repair gaps remain"
    finally:
        engine.dispose()


def test_historical_only_warning_does_not_pause_stock_path():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            db.add(Asset(symbol="SPY", asset_type="stock", is_active=True))
            db.commit()
            historical_only = {
                "status": "incomplete",
                "execution_status": "ready",
                "entitlement_state": "verified",
                "exchange_timestamp": datetime(2026, 10, 9, 14, 59, tzinfo=timezone.utc),
                "provider": "alpaca_iex",
                "feed_class": "iex",
                "missing_intervals": ["2026-10-08T16:33:00+00:00"],
                "unavailable_reason": "Historical repair gaps remain",
            }
            with patch.object(stock_monitoring.settings, "paper_execution_symbols", ["SPY"]), \
                 patch.object(stock_monitoring, "feed_status", return_value=historical_only), \
                 patch.object(stock_monitoring, "_distribution_drift", return_value=[]), \
                 patch.object(stock_monitoring, "_performance_drift", return_value={"key": "model.performance", "category": "model", "metric": "performance", "status": "clear", "message": "clear", "value": {}, "threshold": {}, "details": {}, "action_scope": "model"}), \
                 patch.object(stock_monitoring, "_execution_divergence", return_value={"key": "execution.divergence", "category": "execution", "metric": "divergence", "status": "unknown", "message": "unknown", "value": {}, "threshold": {}, "details": {}, "action_scope": "safety"}), \
                 patch.object(stock_monitoring, "_stock_risk_metrics", return_value=[]), \
                 patch.object(stock_monitoring, "_broker_reconciliation_health", return_value=[]), \
                 patch.object(stock_monitoring, "_worker_scheduler_health", return_value=[]), \
                 patch.object(stock_monitoring, "_pause_stock_path") as pause:
                result = stock_monitoring.run_stock_monitoring(db)

            data_check = next(check for check in result["checks"] if check["key"] == "data.freshness_provenance")
            assert data_check["status"] == "warning"
            assert result["status"] == "warning"
            assert result["actions"] == []
            pause.assert_not_called()
            assert db.query(StockMonitoringBreach).filter_by(breach_key="data.freshness_provenance").one().status == "cleared"
            recovery = db.query(StockPaperRecoveryState).one()
            assert recovery.status not in {"cooldown", "paused", "revalidation_required"}
    finally:
        engine.dispose()


def test_unverified_incomplete_feed_remains_safety_breach():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            db.add(Asset(symbol="SPY", asset_type="stock", is_active=True))
            db.commit()
            with patch.object(stock_monitoring.settings, "paper_execution_symbols", ["SPY"]), patch.object(
                stock_monitoring,
                "feed_status",
                return_value={"status": "incomplete", "execution_status": "ready", "missing_intervals": ["gap"]},
            ):
                result = stock_monitoring._freshness_and_provenance(db)
            assert result["status"] == "breach"
            assert result["value"]["failed_symbols"] == ["SPY"]
    finally:
        engine.dispose()
