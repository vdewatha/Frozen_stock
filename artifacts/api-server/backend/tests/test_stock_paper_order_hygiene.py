"""Stale unfilled entry cancellation and repeated broker-reject alerting."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from app.core.config import settings
from app.models import Notification
from app.models.stock_paper import StockPaperOrder
from app.services.stock_paper_ledger import (
    alert_repeated_order_rejects,
    cancel_aged_open_entries,
    cancel_stale_exit_entries,
)
from tests.test_entry_protections import _seed_account, sqlite_db  # noqa: F401

UTC = timezone.utc
NOW = datetime(2026, 10, 28, 17, 0, tzinfo=UTC)  # Wed 13:00 ET
BOUNDS = (NOW - timedelta(hours=3, minutes=30), NOW + timedelta(hours=3))


class _Gateway:
    def __init__(self, fail=()):
        self.cancelled, self.fail = [], set(fail)

    def cancel_order(self, broker_order_id):
        if broker_order_id in self.fail:
            raise RuntimeError("broker refused cancel")
        self.cancelled.append(broker_order_id)


def _order(db, account, oid, *, minutes_ago, status="accepted", side="buy", source="scheduled_paper_signal",
           order_class="simple", updated_at=None):
    row = StockPaperOrder(id=oid, account_id=account.id, client_order_id=f"c{oid}", broker_order_id=f"b{oid}",
                          symbol="SPY", side=side, quantity=Decimal("1"), limit_price=Decimal("100"),
                          order_type="limit", time_in_force="day", status=status, source=source,
                          order_class=order_class, submitted_at=NOW - timedelta(minutes=minutes_ago),
                          updated_at=updated_at or NOW)
    db.add(row)
    return row


def test_only_aged_unfilled_autonomous_entries_are_cancelled(sqlite_db):
    db = sqlite_db
    account = _seed_account(db)
    _order(db, account, 1, minutes_ago=45)                                   # aged simple entry
    _order(db, account, 2, minutes_ago=40, order_class="bracket")            # aged bracket entry
    _order(db, account, 3, minutes_ago=10)                                   # fresh
    _order(db, account, 4, minutes_ago=45, status="partially_filled")        # has fills
    _order(db, account, 5, minutes_ago=45, side="sell")                      # exit
    _order(db, account, 6, minutes_ago=45, source="manual_close")            # not autonomous
    _order(db, account, 7, minutes_ago=60 * 24)                              # prior session: other sweep
    db.commit()
    gateway = _Gateway()
    with patch("app.services.stock_paper_ledger.session_bounds", return_value=BOUNDS), \
         patch.object(settings, "paper_stale_entry_cancel_minutes", 30):
        cancelled = cancel_aged_open_entries(db, account, NOW, gateway)
    assert cancelled == [1, 2]
    assert gateway.cancelled == ["b1", "b2"]
    assert {row.id: row.status for row in db.query(StockPaperOrder).all()} == {
        1: "pending_cancel", 2: "pending_cancel", 3: "accepted", 4: "partially_filled",
        5: "accepted", 6: "accepted", 7: "accepted"}


def test_cancel_failure_is_left_for_reconciliation_and_zero_disables(sqlite_db):
    db = sqlite_db
    account = _seed_account(db)
    _order(db, account, 1, minutes_ago=45)
    _order(db, account, 2, minutes_ago=45)
    db.commit()
    with patch("app.services.stock_paper_ledger.session_bounds", return_value=BOUNDS):
        assert cancel_aged_open_entries(db, account, NOW, _Gateway(), max_open_minutes=0) == []
        gateway = _Gateway(fail={"b1"})
        assert cancel_aged_open_entries(db, account, NOW, gateway, max_open_minutes=30) == [2]
    assert db.get(StockPaperOrder, 1).status == "accepted"


def test_stale_sweep_entry_point_includes_aged_same_session_entries(sqlite_db):
    db = sqlite_db
    account = _seed_account(db)
    _order(db, account, 1, minutes_ago=45)
    db.commit()
    gateway = _Gateway()
    with patch("app.services.stock_paper_ledger.session_bounds", return_value=BOUNDS), \
         patch.object(settings, "paper_stale_entry_cancel_minutes", 30):
        assert cancel_stale_exit_entries(db, account, NOW, gateway) == [1]


def test_repeated_rejects_raise_one_warning_notification(sqlite_db):
    db = sqlite_db
    account = _seed_account(db)
    _order(db, account, 1, minutes_ago=50, status="rejected")
    _order(db, account, 2, minutes_ago=40, status="rejected")
    _order(db, account, 3, minutes_ago=60 * 30, status="rejected", updated_at=NOW - timedelta(hours=30))
    db.commit()
    with patch.object(settings, "paper_order_reject_alert_count", 3):
        assert alert_repeated_order_rejects(db, account, NOW) is None  # 2 inside 24h
        _order(db, account, 4, minutes_ago=5, status="rejected")
        db.commit()
        first = alert_repeated_order_rejects(db, account, NOW)
        second = alert_repeated_order_rejects(db, account, NOW)
    assert first["rejected_orders_24h"] == 3 and first["alerted"] is True
    assert second["alerted"] is False
    notes = db.query(Notification).filter(Notification.source == "stock_paper_order_rejects").all()
    assert len(notes) == 1 and notes[0].severity == "warning"
    with patch.object(settings, "paper_order_reject_alert_count", 0):
        assert alert_repeated_order_rejects(db, account, NOW) is None
