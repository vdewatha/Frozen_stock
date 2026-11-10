"""A fractional-share fill's cent rounding reconciles instead of halting the account.

On 2026-11-10 the slow book's first tranche filled 6.1512 VOO and 167.957 SGOV.
Alpaca settled each order's cash to the cent while the ledger replayed the
fills at full precision, so the exact policy saw a $0.00084 residual and
halted the account as "unexplained", operator-only, for the rest of the session.
"""
from decimal import Decimal

from app.core.config import settings
from app.models import StockPaperRecoveryEvent, StockPaperRecoveryState
from app.models.stock_paper import StockPaperAccount, StockPaperLedgerEvent
from app.services.paper_cash_policy import EXACT
from app.services.stock_paper_ledger import reconcile_stock_paper_account
from app.services.stock_recovery import classify_recovery_halt
from tests.test_alpaca_activity_ledger import fill_event, initialize, ledger, report, set_position  # noqa: F401

QTY, PRICE = Decimal("0.1512"), Decimal("686")   # the VOO remainder fill
GROSS = QTY * PRICE                              # 103.7232, settled as 103.72


def fractional_fill_posted(broker):
    """Alpaca view after the fill: cash to the cent, position at full precision."""
    broker.cash = str(Decimal("1000") - GROSS.quantize(Decimal("0.01")))  # 896.28
    set_position(broker, qty=str(QTY))
    broker.rows.append(fill_event(qty=str(QTY), price=str(PRICE)))
    broker.order_rows = [{"id": "order", "client_order_id": "tw-order", "symbol": "SPY", "side": "buy",
                          "qty": str(QTY), "filled_qty": str(QTY), "filled_avg_price": str(PRICE),
                          "status": "filled", "type": "limit", "time_in_force": "day"}]


def test_fractional_fill_reconciles_under_the_exact_policy(ledger):
    db, broker = ledger
    initialize(db, broker)
    fractional_fill_posted(broker)
    result = reconcile_stock_paper_account(db, broker)
    assert result["status"] == "reconciled"
    assert not result["account"]["unexplained_residual"]
    assert not result["account"]["accounting_verified"] and not result["costs_known"]
    paid = report(db)
    assert paid["status"] == "matched" and paid["cash_rounding_explained"] is True
    assert paid["cash_policy"] == EXACT
    assert Decimal(paid["unrounded_cash_residual"]) == Decimal("0.0032")
    assert Decimal(paid["cash_rounding_adjustment"]) == Decimal("0.0032")
    assert paid["launch_authorized"] is False and paid["all_in_costs"] == "unknown"
    assert db.query(StockPaperAccount).one().cash_policy == EXACT
    # Reconciling again is a plain matched replay, no new residual.
    assert reconcile_stock_paper_account(db, broker)["status"] == "reconciled"


def test_a_real_cent_still_halts(ledger):
    db, broker = ledger
    initialize(db, broker)
    fractional_fill_posted(broker)
    broker.cash = "896.27"  # one cent the rounding does not explain
    result = reconcile_stock_paper_account(db, broker)
    assert result["status"] == "halted" and result["account"]["unexplained_residual"]


def test_switch_off_restores_the_halt(ledger, monkeypatch):
    db, broker = ledger
    initialize(db, broker)
    fractional_fill_posted(broker)
    monkeypatch.setattr(settings, "alpaca_paper_cent_rounding_explained", False)
    result = reconcile_stock_paper_account(db, broker)
    assert result["status"] == "halted" and result["account"]["unexplained_residual"]


def test_explained_rounding_clears_an_existing_residual_halt(ledger):
    """The deployed account on 2026-11-10 13:02 ET: halted on the residual, paused by the monitor."""
    db, broker = ledger
    initialize(db, broker)
    broker.cash = "999"  # unexplained at first
    assert reconcile_stock_paper_account(db, broker)["status"] == "halted"
    state = db.get(StockPaperRecoveryState, 1)
    assert state.accounting_review_required
    state.status = "cooldown"
    db.add(StockPaperRecoveryEvent(
        action="pause", status="paused", actor="stock_monitor", reason="monitor breach", event_sha256="b" * 64,
        payload={"monitor_breach_keys": ["health.broker", "health.reconciliation"],
                 "prior_status": "armed", "prior_kill_switch": False},
    ))
    db.commit()
    assert not classify_recovery_halt(db)["explained"]
    fractional_fill_posted(broker)  # the only remaining gap is the cent rounding
    result = reconcile_stock_paper_account(db, broker)
    assert result["status"] == "reconciled"
    account = db.query(StockPaperAccount).one()
    assert not account.unexplained_residual and account.halt_reason is None
    db.refresh(state)
    assert not state.accounting_review_required
    assert "cent rounding" in state.accounting_review_reason
    payload = db.query(StockPaperLedgerEvent).filter_by(event_type="accounting_residual_reclassified").one().payload
    assert payload["accounting_review_cleared"] is True
    assert Decimal(payload["cash_rounding_adjustment"]) == Decimal("0.0032")
    classified = classify_recovery_halt(db)
    assert classified["explained"] and classified["class"] == "transient_broker"
