"""Close a verified paper probe review, never authorize strategy execution."""
import hashlib
import json
from uuid import UUID

from app.models import StockPaperLedgerEvent, StockPaperOrder
from app.services.paper_cash_policy import MONETARY_REVIEW_REASON
from app.services.stock_paper_ledger import PROBE_HALT, active_paper_account, _event

VERSION = "paper-probe-completion-v1"


def _digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def completed_review(db, reservation):
    rows = db.query(StockPaperLedgerEvent).filter_by(account_id=reservation.account_id,
        event_type="paper_probe_completion_review", status="reviewed").filter(
        StockPaperLedgerEvent.id > reservation.id).order_by(StockPaperLedgerEvent.id.desc()).all()
    for row in rows:
        report = row.payload or {}
        body = {key: value for key, value in report.items() if key != "report_sha256"}
        if (report.get("version") == VERSION and report.get("scope") == "completed_probe_only"
                and report.get("reservation_id") == reservation.id
                and report.get("run_id") == reservation.payload.get("run_id")
                and report.get("symbol") == reservation.payload.get("symbol")
                and report.get("execution_authorized") is False and report.get("live_authorized") is False
                and report.get("costs_verified") is False and report.get("automatic_resume") is False
                and (report.get("broker_observation") or {}).get("status") == "observed_flat"
                and (report.get("accounting") or {}).get("accounting_observation_ready") is True
                and (report.get("accounting") or {}).get("scope") == "probe_completion_observation_only"
                and report.get("reviewed_by") == row.actor and bool(row.actor and row.actor.strip())
                and report.get("report_sha256") == _digest(body)):
            return row
    return None


def pending_probe_reservation(db, account):
    reservations = db.query(StockPaperLedgerEvent).filter_by(account_id=account.id,
        event_type="paper_probe_reservation", status="reserved").order_by(StockPaperLedgerEvent.id.desc()).all()
    return next((row for row in reservations if completed_review(db, row) is None), None)


def review(db, client, content, *, run_id, symbol, actor, apply=False):
    from app.services.paper_research_accounting import _assess
    from scripts.recover_paper_probe import recover

    run_id = str(UUID(run_id))
    if not actor.strip() or not content or len(content.encode()) > 1_000_000 or not content.endswith("\n"):
        raise ValueError("Identified reviewer and a complete bounded journal are required")
    events = [json.loads(line) for line in content.splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in events):
        raise ValueError("Invalid journal event")
    account = active_paper_account(db, for_update=True)
    if account is None:
        raise ValueError("An existing paper account is required")
    reservation = db.query(StockPaperLedgerEvent).filter_by(account_id=account.id,
        event_type="paper_probe_reservation", status="reserved").order_by(StockPaperLedgerEvent.id.desc()).first()
    if (not reservation or reservation.payload.get("run_id") != run_id
            or reservation.payload.get("symbol") != symbol or reservation.payload.get("buy_notional_cap") != "10"
            or reservation.payload.get("paper_only") is not True or reservation.payload.get("live_authorized") is not False):
        raise ValueError("The exact latest bounded paper reservation is required")
    existing = completed_review(db, reservation)
    if existing:
        return {"status": "already_reviewed", "review_event_id": existing.id,
                "execution_authorized": False, "live_authorized": False, "state_changed": False}
    if account.status != "halted" or account.halt_reason != PROBE_HALT:
        raise ValueError("The probe halt must be isolated before completion review")
    reserved = [row for row in events if row.get("event") == "probe_reserved"]
    if (len(reserved) != 1 or reserved[0].get("run_id") != run_id or reserved[0].get("symbol") != symbol
            or reserved[0].get("paper_only") is not True or reserved[0].get("automatic_resume") is not False):
        raise ValueError("Journal reservation does not match the ledger")
    accounting = _assess(db, account, probe_review=True)
    if not accounting["accounting_observation_ready"]:
        raise ValueError(accounting["reason"])
    def reject_write(event):
        raise RuntimeError("Completion inspection must never mutate the broker journal")
    observation = recover(client, run_id, account.broker_account_id, events, reject_write, symbol=symbol)
    if observation["status"] != "observed_flat":
        raise ValueError("Only a confirmed filled-and-flat probe can be completed")
    intents = [row["payload"] for row in events if row.get("event") == "before_submit"]
    evidence = []
    for payload in intents:
        broker = client.order_by_client_id(payload["client_order_id"])
        order = db.query(StockPaperOrder).filter_by(account_id=account.id, client_order_id=payload["client_order_id"]).one_or_none()
        if not order or not broker or order.raw_payload != broker:
            raise ValueError("Fresh probe order evidence differs from the reconciled ledger")
        evidence.append({"client_order_id": order.client_order_id, "broker_order_id": order.broker_order_id,
                         "status": order.status, "filled_qty": broker["filled_qty"]})
    current = client.account()
    from app.services.alpaca_activity_v2 import amount
    if (current.get("id") != account.broker_account_id or amount(current.get("cash")) != account.cash
            or amount(current.get("equity")) != account.equity or client.positions()
            or current.get("status") != "ACTIVE" or current.get("currency") != "USD"
            or any(current.get(key) is not False for key in ("trading_blocked", "account_blocked", "trade_suspended_by_user"))):
        raise ValueError("Broker balances changed after reconciliation")
    current_orders = client.orders()
    known_orders = db.query(StockPaperOrder).filter_by(account_id=account.id).all()
    if (len(current_orders) != len(known_orders)
            or len({row.get("id") for row in current_orders}) != len(current_orders)
            or {row.get("id"): row for row in current_orders} != {row.broker_order_id: row.raw_payload for row in known_orders}):
        raise ValueError("Broker order history changed after reconciliation")
    if _assess(db, account, probe_review=True) != accounting:
        raise ValueError("Accounting evidence expired or changed during review")
    report = {"version": VERSION, "scope": "completed_probe_only", "reservation_id": reservation.id,
        "run_id": run_id, "symbol": symbol, "journal_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "accounting": accounting, "broker_observation": observation, "orders": evidence,
        "reviewed_by": actor.strip(), "costs_verified": False, "automatic_resume": False,
        "execution_authorized": False, "live_authorized": False}
    report["report_sha256"] = _digest(report)
    if apply:
        db.info["stock_paper_actor"] = actor.strip()
        _event(db, account, "paper_probe_completion_review", "reviewed", MONETARY_REVIEW_REASON, report)
        db.flush()
        if pending_probe_reservation(db, account) is None:
            account.halt_reason = MONETARY_REVIEW_REASON
    return {"status": "reviewed" if apply else "preview", "apply": apply, "report": report}
