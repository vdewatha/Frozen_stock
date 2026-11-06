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


# ------------------------------------------------------------ change narrative

def _audit(event_type, action, status, message, payload, *, at, entity_id=None):
    """A sample audit row as the narrative sees it (no database)."""
    return {"event_type": event_type, "action": action, "status": status, "message": message, "payload": payload,
            "created_at": at, "entity_id": entity_id}


def test_change_sentences_are_plain_language_over_sample_payloads():
    names = {9: "RSI(2) Pullback", 4: "Channel Breakout"}
    rows = [
        _audit("strategy_governance", "pause", "complete",
               "Paper-trading evidence shows a losing expectancy over 40 trades.",
               {"old_status": "paper_trading_active", "new_status": "paused"},
               at=datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc), entity_id=9),
        _audit("strategy_governance", "hold", "complete", "Paper evidence reviewed; no lifecycle change.",
               {"old_status": "paper_trading_active", "new_status": "paper_trading_active"},
               at=datetime(2026, 11, 2, 21, 1, tzinfo=timezone.utc), entity_id=9),
        _audit("strategy_governance", "market_gate_update", "complete",
               "experiment ab12cd: no new longs while SPY is at or below its 20-day average; adopted by the "
               "experiment engine after the missed_market_move lesson repeated and the pre-registered backtest "
               "rule passed. Sells and exits are unchanged.",
               {"market_gate_sma": 20, "source": "experiment_engine", "change_id": "ab12cd", "paper_only": True},
               at=datetime(2026, 11, 1, 17, 0, tzinfo=timezone.utc), entity_id=4),
        _audit("experiment_engine", "run", "complete", "experiment engine: 3 lessons, 2 experiments, 1 adopted",
               {"lessons": 3, "experiments": [{}, {}], "adopted": ["ab12cd"], "untemplated": ["stop_cooldown"]},
               at=datetime(2026, 11, 1, 17, 1, tzinfo=timezone.utc)),
        _audit("experiment_engine", "experiment", "adopted", "channel_breakout / market_gate",
               {"template": "market_gate", "strategy_type": "channel_breakout", "trigger": "missed_market_move",
                "lesson_count": 4, "adopted": "sma20",
                "adopted_description": "no new longs while SPY is at or below its 20-day average"},
               at=datetime(2026, 11, 1, 17, 0, 30, tzinfo=timezone.utc), entity_id=4),
        _audit("experiment_engine", "experiment", "rejected", "rsi2_pullback / vix_gate",
               {"template": "vix_gate", "strategy_type": "rsi2_pullback", "trigger": "high_vix_entry",
                "lesson_count": 3, "adopted": None},
               at=datetime(2026, 11, 1, 17, 0, 40, tzinfo=timezone.utc), entity_id=9),
        _audit("experiment_engine", "change_adopted", "watching", "channel_breakout: ... (change ab12cd)",
               {"change_id": "ab12cd", "strategy_type": "channel_breakout", "template": "market_gate",
                "trigger": "missed_market_move", "description": "no new longs while SPY is at or below its 20-day average",
                "recent_before": {"trades": 12, "expectancy_bps": -8.5}, "watch": {"min_trades": 10}},
               at=datetime(2026, 11, 1, 17, 0, 50, tzinfo=timezone.utc), entity_id=4),
        _audit("experiment_engine", "change_verdict", "reverted", "channel_breakout: ... -> not_helping",
               {"change_id": "ab12cd", "strategy_type": "channel_breakout", "template": "market_gate",
                "trigger": "missed_market_move", "verdict": "not_helping", "state": "reverted", "reverted": True,
                "before": {"trades": 12, "expectancy_bps": -8.5}, "after": {"trades": 11, "expectancy_bps": -31.0}},
               at=datetime(2026, 11, 5, 22, 10, tzinfo=timezone.utc), entity_id=4),
        _audit("stock_paper", "gate_cost", "recorded", "gate costs through 2026-11-04",
               {"report": {"as_of": "2026-11-04", "window": {"sessions": 60},
                           "totals": {"blocked": 41, "scored": 30, "open": 6},
                           "by_strategy": [{"gate": "market_gate", "strategy_type": "channel_breakout",
                                            "verdict": "costly", "mean_net_bps": 22.0}]}},
               at=datetime(2026, 11, 4, 22, 20, tzinfo=timezone.utc)),
        _audit("stock_paper", "gate_cost", "recorded", "gate costs through 2026-11-05",
               {"report": {"as_of": "2026-11-05", "window": {"sessions": 60},
                           "totals": {"blocked": 44, "scored": 33, "open": 5},
                           "by_strategy": [{"gate": "market_gate", "strategy_type": "channel_breakout",
                                            "verdict": "costly", "mean_net_bps": 19.0},
                                           {"gate": "earnings_blackout", "strategy_type": "rsi2_pullback",
                                            "verdict": "earning its keep", "mean_net_bps": -14.0},
                                           {"gate": "vix_gate", "strategy_type": "rsi2_pullback",
                                            "verdict": "too early", "mean_net_bps": 3.0}]}},
               at=datetime(2026, 11, 5, 22, 20, tzinfo=timezone.utc)),
        _audit("self_healing", "run", "clear", "Self-healing found nothing to fix.", {},
               at=datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)),
        _audit("self_healing", "stale_intraday_symbols", "remediated",
               "Self-healing found 2 traded symbols have no minute bar from the last 15 minutes while SPY is "
               "current: SOFI, AAPL; it re-ran the intraday backfill for SOFI, AAPL within the 25-second repair "
               "budget; 2 of 2 came back complete.", {},
               at=datetime(2026, 11, 6, 15, 15, tzinfo=timezone.utc)),
    ]
    items = weekly.narrate_changes(rows, strategy_names=names)
    assert [item["at"][:16] for item in items] == sorted(item["at"][:16] for item in items)
    sentences = {(item["source"], item["action"]): item["sentence"] for item in items}
    assert sentences[("strategy_governance", "pause")] == (
        "On Nov 2, the bot paused RSI(2) Pullback: paper-trading evidence shows a losing expectancy over 40 trades.")
    assert ("strategy_governance", "hold") not in sentences  # a daily review note is not a change
    assert sentences[("strategy_governance", "market_gate_update")].startswith(
        "On Nov 1, the experiment engine set market_gate_sma = 20 on Channel Breakout (change ab12cd): "
        "experiment ab12cd: no new longs while SPY is at or below its 20-day average; adopted by the experiment "
        "engine after the missed_market_move lesson repeated")
    assert sentences[("experiment_engine", "run")] == (
        "On Nov 1, the experiment engine reviewed 3 repeating lessons, ran 2 experiments and adopted 1 changes; "
        "1 lessons have no experiment template yet.")
    adopted = [item["sentence"] for item in items if item["action"] == "experiment"]
    assert adopted[0] == ("On Nov 1, the engine tested the market gate on channel_breakout because missed market "
                          "move repeated 4 times and adopted 'no new longs while SPY is at or below its 20-day "
                          "average' under the pre-registered rule.")
    assert adopted[1] == ("On Nov 1, the engine tested the vix gate on rsi2_pullback because high vix entry "
                          "repeated 3 times; no candidate beat the current setup, so nothing changed.")
    assert sentences[("experiment_engine", "change_adopted")] == (
        "On Nov 1, change ab12cd on channel_breakout went live: no new longs while SPY is at or below its 20-day "
        "average. Before it, the last 12 trades averaged -8.5 bps per trade. So far: judged not helping after "
        "11 trades.")
    assert sentences[("experiment_engine", "change_verdict")] == (
        "On Nov 5, after 11 trades, change ab12cd on channel_breakout (market gate for missed market move) was "
        "judged not helping: -31.0 bps per trade against -8.5 bps per trade before; the previous setup was restored.")
    gate = [item for item in items if item["source"] == "gate_cost"]
    assert len(gate) == 1  # only the latest report of the window
    assert gate[0]["sentence"] == (
        "Through 2026-11-05, the gate-cost audit scored 33 of the 44 BUYs the entry gates withheld over the last "
        "60 sessions; costly so far: market gate on channel_breakout (+19 bps per blocked trade); earning their "
        "keep: earnings blackout on rsi2_pullback (-14 bps per blocked trade).")
    healing = [item for item in items if item["source"] == "self_healing"]
    assert len(healing) == 1  # run rows are bookkeeping, not changes
    assert healing[0]["sentence"].startswith("On Nov 6 at 10:15 ET, self-healing found 2 traded symbols have no "
                                             "minute bar")
    assert healing[0]["sentence"].endswith("2 of 2 came back complete.")


def test_change_sentences_cope_with_thin_payloads():
    rows = [
        _audit("strategy_governance", "retire", "complete", "", {"old_status": "paused", "new_status": "retired"},
               at=datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc), entity_id=77),
        _audit("strategy_governance", "vix_gate_update", "complete", "experiment 99 reverted: not helping.",
               {"vix_gate": None, "vix_level": None, "source": "experiment_engine", "reverted_change": "99"},
               at=datetime(2026, 11, 3, 12, 1, tzinfo=timezone.utc), entity_id=77),
        _audit("experiment_engine", "change_adopted", "watching", "", {"change_id": "z1", "strategy_type": "x"},
               at=datetime(2026, 11, 3, 12, 2, tzinfo=timezone.utc)),
        _audit("stock_paper", "gate_cost", "recorded", "", {"report": {}},
               at=datetime(2026, 11, 3, 12, 3, tzinfo=timezone.utc)),
        _audit("experiment_engine", "something_new", "complete", "", {}, at=None),
    ]
    sentences = [item["sentence"] for item in weekly.narrate_changes(rows)]
    assert sentences == [
        "On Nov 3, the bot retired strategy #77: it is now retired.",
        "On Nov 3, the experiment engine reverted change 99 on strategy #77: experiment 99 reverted: not helping.",
        "On Nov 3, change z1 on x went live: None. Before it, the last 0 trades averaged no measured return. "
        "So far: watching.",
        "Through Nov 3, the gate-cost audit scored 0 of the 0 BUYs the entry gates withheld over the last ? "
        "sessions; no gate is costing more than it saves yet.",
    ]


def test_report_carries_the_weeks_changes_from_the_audit_log(db):
    def stamp(row, at):
        db.flush()
        row.created_at = at.replace(tzinfo=None)
        db.commit()

    stamp(write_audit_log(db, event_type="strategy_governance", entity_type="strategy", entity_id=9, action="pause",
                          status="complete", message="Losing expectancy.",
                          payload={"old_status": "paper_trading_candidate", "new_status": "paused"}),
          NOW - timedelta(days=2))
    stamp(write_audit_log(db, event_type="strategy_governance", entity_type="strategy", entity_id=3, action="pause",
                          status="complete", message="Too old to count.",
                          payload={"old_status": "paper_trading_active", "new_status": "paused"}),
          NOW - timedelta(days=9))
    stamp(write_audit_log(db, event_type="self_healing", action="expired_learning_lease", status="remediated",
                          message="Self-healing found 1 learning scope past its lease; it requeued it.",
                          payload={}), NOW - timedelta(days=1))
    report = weekly.build_weekly_report(db, now=NOW)
    changes = report["changes"]
    assert changes["counts"] == {"strategy_governance": 1, "self_healing": 1}
    assert changes["items"][0]["sentence"] == "On Nov 5, the bot paused RSI(2) Pullback: losing expectancy."
    assert changes["items"][1]["sentence"].startswith("On Nov 6 at ")
    assert "language model" in changes["method"]
