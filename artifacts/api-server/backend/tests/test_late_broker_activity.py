"""Late broker activity: hold instead of halt, and clear the review flag on an exact replay.

On 2026-11-09 Alpaca moved cash and positions for a QQQ take-profit about 25
minutes before it posted the FILL activity; every reconcile in between halted
the account, and the recovery pause then needed an operator Resume.
"""
from datetime import datetime, timedelta, timezone

from app.models import StockPaperRecoveryEvent, StockPaperRecoveryState
from app.models.stock_paper import StockPaperAccount, StockPaperLedgerEvent
from app.services.stock_paper_ledger import reconcile_stock_paper_account
from app.services.stock_recovery import classify_recovery_halt
from tests.test_alpaca_activity_ledger import fill_event, initialize, ledger, report, set_position  # noqa: F401

UTC = timezone.utc


def filled_order(minutes_ago, **changes):
    filled_at = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()
    return {"id": "order", "client_order_id": "ext-order", "symbol": "SPY", "side": "buy", "qty": "2",
            "filled_qty": "2", "filled_avg_price": "100", "filled_at": filled_at, "created_at": filled_at,
            "status": "filled", "type": "market", "time_in_force": "day", **changes}


def broker_moved_before_activity(broker, minutes_ago=10, **order_changes):
    """Cash and position reflect a 2 x $100 buy; the FILL activity is not posted yet."""
    broker.cash = "800"
    set_position(broker)
    broker.order_rows = [filled_order(minutes_ago, **order_changes)]


def events(db, kind):
    return db.query(StockPaperLedgerEvent).filter_by(event_type=kind).count()


def test_recent_fill_without_activity_is_held_not_halted(ledger):
    db, broker = ledger
    initialize(db, broker)
    broker_moved_before_activity(broker)
    result = reconcile_stock_paper_account(db, broker)
    assert result["status"] == "reconciled"
    assert not result["account"]["unexplained_residual"]
    assert not result["account"]["accounting_verified"]
    assert report(db)["late_activity"]["pending_fills"][0]["order_id"] == "order"
    assert events(db, "activity_pending") == 1
    # The activity posts: the replay now matches on its own.
    broker.rows.append(fill_event())
    result = reconcile_stock_paper_account(db, broker)
    assert result["status"] == "reconciled"
    assert report(db)["status"] == "matched"
    assert "late_activity" not in report(db)


def test_fill_older_than_the_grace_still_halts(ledger):
    db, broker = ledger
    initialize(db, broker)
    broker_moved_before_activity(broker, minutes_ago=180)
    result = reconcile_stock_paper_account(db, broker)
    assert result["status"] == "halted"
    assert result["account"]["unexplained_residual"]
    assert events(db, "activity_pending") == 0


def test_gap_not_exactly_explained_still_halts(ledger):
    db, broker = ledger
    initialize(db, broker)
    broker_moved_before_activity(broker)
    broker.cash = "799.99"  # one cent the pending fill does not explain
    assert reconcile_stock_paper_account(db, broker)["status"] == "halted"
    assert events(db, "activity_pending") == 0


def test_exact_replay_clears_the_review_flag_and_lets_auto_resume_classify_it(ledger):
    db, broker = ledger
    initialize(db, broker)
    broker.cash = "999"  # unexplained, no order to explain it
    assert reconcile_stock_paper_account(db, broker)["status"] == "halted"
    state = db.get(StockPaperRecoveryState, 1)
    assert state.accounting_review_required
    # The monitor paused on its broker/reconciliation health checks (an explained class).
    state.status = "cooldown"
    db.add(StockPaperRecoveryEvent(
        action="pause", status="paused", actor="stock_monitor", reason="monitor breach", event_sha256="a" * 64,
        payload={"monitor_breach_keys": ["health.broker", "health.reconciliation"],
                 "prior_status": "armed", "prior_kill_switch": False},
    ))
    db.commit()
    assert not classify_recovery_halt(db)["explained"]
    broker.cash = "1000"  # the late evidence arrives and the replay is exact
    result = reconcile_stock_paper_account(db, broker)
    assert result["status"] == "reconciled"
    db.refresh(state)
    assert not state.accounting_review_required
    assert state.accounting_reviewed_by == "stock_reconciler"
    payload = db.query(StockPaperLedgerEvent).filter_by(event_type="accounting_residual_reclassified").one().payload
    assert payload["accounting_review_cleared"] is True
    classified = classify_recovery_halt(db)
    assert classified["explained"] and classified["class"] == "transient_broker"


def test_review_flag_untouched_when_no_exact_replay(ledger):
    db, broker = ledger
    initialize(db, broker)
    broker.cash = "999"
    reconcile_stock_paper_account(db, broker)
    reconcile_stock_paper_account(db, broker)
    assert db.get(StockPaperRecoveryState, 1).accounting_review_required
    assert db.query(StockPaperAccount).one().unexplained_residual
