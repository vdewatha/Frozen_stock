"""Weekly paper performance report; the scorecard is stubbed, the database is real."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import AuditLog, Notification, Strategy
from app.services import weekly_report as weekly
from app.services.audit import write_audit_log

# Saturday 2026-11-07, 9:07 ET.
NOW = datetime(2026, 11, 7, 14, 7, tzinfo=timezone.utc)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            Strategy(id=9, name="RSI(2) Pullback", strategy_type="rsi2_pullback", parameters={},
                     is_active=True, current_status="paper_trading_candidate"),
            Strategy(id=3, name="MACD Momentum", strategy_type="macd_momentum", parameters={},
                     is_active=True, current_status="retired"),
        ])
        session.commit()

        def scorecard(_db, *, days, now, classify):
            rows = {9: {"key": 9, "strategy_name": "RSI(2) Pullback", "trades": 34 if days > 7 else 4,
                        "win_rate": 0.62, "expectancy": 3.1, "total_pnl": 105.4 if days > 7 else 12.0,
                        "expectancy_return": 0.0009},
                    3: {"key": 3, "strategy_name": "MACD Momentum", "trades": 2, "win_rate": 0.5,
                        "expectancy": -4.0, "total_pnl": -8.0, "expectancy_return": -0.001}}
            by_strategy = [rows[9], rows[3]] if days > 7 else [rows[9]]
            trades = sum(row["trades"] for row in by_strategy)
            return {"status": "available", "by_strategy": by_strategy,
                    "headline": {"trades": trades, "win_rate": 0.6, "total_pnl": sum(r["total_pnl"] for r in by_strategy),
                                 "expectancy": 2.7, "expectancy_return": 0.0008},
                    "open_mark_to_market": {"unrealized_pnl": 14.5}}

        monkeypatch.setattr(weekly, "build_scorecard", scorecard)
        monkeypatch.setattr(weekly, "_short_pilot_summary", lambda _db: None)
        yield session
    engine.dispose()


def test_verdict_waits_for_evidence_then_compares_to_backtest():
    assert weekly.verdict(12, 40.0, 15.0) == "needs 18 more closed trades"
    assert weekly.verdict(30, -2.0, 15.0) == "losing money"
    assert weekly.verdict(30, 5.0, 15.0) == "profitable but below half its backtest"
    assert weekly.verdict(30, 9.0, 15.0) == "on track with its backtest"


def test_report_puts_paper_results_next_to_the_backtest(db):
    report = weekly.build_weekly_report(db, now=NOW)
    assert report["week"] == "2026-W45"
    rsi2, macd = report["strategies"]
    assert rsi2["strategy_type"] == "rsi2_pullback" and rsi2["trades"] == 34 and rsi2["week_trades"] == 4
    assert rsi2["paper_bps"] == 9.0 and rsi2["backtest_bps"] == 15.0
    assert rsi2["verdict"] == "on track with its backtest"
    assert macd["status"] == "retired" and macd["week_trades"] == 0 and macd["verdict"].startswith("needs")
    assert report["this_week"]["trades"] == 4 and report["all_time"]["trades"] == 36
    assert report["mutates_strategy_state"] is False


def test_blocked_reasons_are_grouped_and_counted_once(db):
    def snapshot(at, outcomes):
        write_audit_log(db, event_type="stock_paper", action=weekly.BUY_OUTCOME_ACTION, status="recorded",
                        message="BUY signal outcomes changed", payload={"outcomes": outcomes})
        db.flush()
        db.query(AuditLog).order_by(AuditLog.id.desc()).first().created_at = at.replace(tzinfo=None)
        db.commit()

    earnings = {"symbol": "AAPL", "strategy": "rsi2_pullback", "status": "skipped",
                "reason": "earnings on 2026-11-04 fall within the 10-session hold window"}
    snapshot(NOW - timedelta(days=3), [earnings])
    snapshot(NOW - timedelta(days=3, minutes=-1), [earnings, {"symbol": "NVDA", "strategy": "rsi2_pullback",
              "status": "skipped", "reason": "earnings on 2026-11-18 fall within the 10-session hold window"}])
    snapshot(NOW - timedelta(days=2), [{"symbol": "SPY", "strategy": "ibs_mean_reversion", "status": "submitted",
                                        "reason": None}])
    snapshot(NOW - timedelta(days=9), [{"symbol": "MSFT", "strategy": "rsi2_pullback", "status": "skipped",
                                        "reason": "too old to count"}])
    result = weekly.blocked_buy_reasons(db, NOW - timedelta(days=7), NOW)
    assert result["submitted_buys"] == 1 and result["blocked_buys"] == 2
    assert result["top_reasons"] == [
        {"reason": "earnings on <date> fall within the <n>-session hold window", "count": 2}]


def test_report_is_stored_once_per_week_with_a_notification(db):
    first = weekly.record_weekly_report(db, now=NOW)
    again = weekly.record_weekly_report(db, now=NOW + timedelta(hours=5))
    assert first["recorded"] is True and again["recorded"] is False
    assert db.query(AuditLog).filter_by(action=weekly.REPORT_ACTION).count() == 1
    assert weekly.latest_weekly_report(db)["week"] == "2026-W45"
    note = db.query(Notification).filter_by(category="weekly_performance_report").one()
    assert note.severity == "info" and "36 trades" in note.message
    assert weekly.record_weekly_report(db, now=NOW + timedelta(days=7))["week"] == "2026-W46"


def test_weekly_report_route_is_viewer_readable():
    from app.core.security import required_role
    from app.tasks.celery_app import celery_app

    assert required_role("GET", "/stock-paper/weekly-report") == "viewer"
    entry = celery_app.conf.beat_schedule["weekly-performance-report"]
    assert entry["task"] == "app.tasks.jobs.weekly_performance_report_job"
