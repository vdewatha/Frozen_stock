from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import Asset
from app.services import stock_monitoring


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
                 patch.object(stock_monitoring, "feed_status", return_value={"status": "ready"}):
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
