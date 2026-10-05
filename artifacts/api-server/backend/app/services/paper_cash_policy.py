"""Explicit monetary comparison policies; never a fee or execution approval."""
from app.services.alpaca_activity_v2 import VERSION, amount
from app.services.alpaca_cash_precision import POLICY as DIAGNOSTIC_VERSION

EXACT = "exact-v1"
CENT = "alpaca-usd-unambiguous-cent-v1"
MONETARY_REVIEW_REASON = "Cash and inventory match the reviewed paper policy; cost qualification and recovery review remain pending"


def apply_policy(report, diagnostic, policy, *, broker, currency):
    if policy not in {EXACT, CENT}:
        raise ValueError("Unknown paper cash policy")
    report = {**report,
              "cash_residual": report.get("unrounded_cash_residual", report["cash_residual"]),
              "cash_equation_matches": report.get("unrounded_cash_equation_matches", report["cash_equation_matches"])}
    report["status"] = "matched" if report["cash_equation_matches"] is True and report["inventory_equation_matches"] is True else "mismatch"
    result = {**report, "cash_policy": policy,
              "unrounded_cash_residual": report["cash_residual"],
              "unrounded_cash_equation_matches": report["cash_equation_matches"],
              "cash_rounding_adjustment": "0"}
    if policy == EXACT:
        return result
    if broker != "alpaca_paper" or currency != "USD":
        raise ValueError("Cent policy is restricted to Alpaca paper USD accounts")
    if report["cash_equation_matches"] is True:
        return result
    if (not isinstance(diagnostic, dict) or diagnostic.get("policy") != DIAGNOSTIC_VERSION
            or diagnostic.get("status") != "consistent"
            or diagnostic.get("inventory_matches") is not True
            or report["inventory_equation_matches"] is not True):
        return result
    raw = amount(report["cash_residual"])
    adjustment = amount(diagnostic["rounding_adjustment_since_baseline"])
    if (amount(diagnostic["unrounded_cash_residual"]) != raw
            or amount(diagnostic["cash_residual_after_rounding"]) != 0
            or raw - adjustment != 0):
        raise ValueError("Cash rounding evidence does not match the reconciliation")
    return {**result, "status": "matched", "cash_equation_matches": True,
            "cash_residual": "0", "cash_rounding_adjustment": format(adjustment, "f"),
            "all_in_costs": "unknown", "launch_authorized": False}


def adopt_cent_policy(db, *, actor, apply=False):
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.core.config import settings
    from app.models import StockPaperLedgerEvent, StockPaperPosition, StockPaperOrder
    from app.services.stock_paper_ledger import active_paper_account, _event, _utc

    if settings.allow_live_trading or settings.active_paper_broker != "alpaca_paper" or not actor.strip():
        raise ValueError("An identified actor and live-disabled Alpaca paper environment are required")
    account = active_paper_account(db, for_update=True)
    if (not account or account.broker != "alpaca_paper" or account.currency != "USD"
            or account.activity_contract != VERSION or account.cash_policy not in {EXACT, CENT}):
        raise ValueError("A matching USD paper account is required")
    event = db.scalar(select(StockPaperLedgerEvent).where(
        StockPaperLedgerEvent.account_id == account.id,
        StockPaperLedgerEvent.event_type == "activity_reconciliation",
    ).order_by(StockPaperLedgerEvent.id.desc()))
    now = datetime.now(timezone.utc)
    if (not event or not account.last_reconciled_at
            or not 0 <= (now - _utc(event.created_at)).total_seconds() <= 120
            or not 0 <= (now - _utc(account.last_reconciled_at)).total_seconds() <= 120):
        raise ValueError("Fresh broker reconciliation is required before policy adoption")
    if db.query(StockPaperPosition).filter_by(account_id=account.id).count():
        raise ValueError("Policy adoption requires a flat paper account")
    if db.query(StockPaperOrder).filter(StockPaperOrder.account_id == account.id,
        StockPaperOrder.status.notin_(["filled", "canceled", "expired", "rejected", "replaced"])).count():
        raise ValueError("Policy adoption requires no pending orders")
    report = event.payload or {}
    baseline = account.activity_baseline or {}
    if (report.get("version") != VERSION or baseline.get("version") != VERSION
            or report.get("baseline_journal_sha256") != baseline.get("journal_sha256")
            or not baseline.get("journal_sha256")):
        raise ValueError("Reconciliation does not match the frozen baseline")
    diagnostic = report.get("cash_precision_diagnostic")
    if not isinstance(diagnostic, dict) or diagnostic.get("status") != "consistent":
        raise ValueError("Unambiguous cent evidence is required")
    candidate = apply_policy(report, diagnostic, CENT, broker=account.broker, currency=account.currency)
    if candidate["status"] != "matched":
        raise ValueError("Cash and inventory must match under the proposed policy")
    result = {"policy": CENT, "prior_policy": account.cash_policy, "apply": apply,
              "journal_sha256": report["journal_sha256"],
              "baseline_journal_sha256": baseline["journal_sha256"],
              "reconciliation_event_id": event.id, "launch_authorized": False,
              "costs_verified": False, "halt_cleared": False}
    if apply and account.cash_policy != CENT:
        account.cash_policy = CENT
        db.info["stock_paper_actor"] = actor.strip()
        _event(db, account, "cash_policy_adopted", "recorded",
               "Explicit unambiguous cent policy; baseline, costs and trading controls unchanged", result)
    return result


def review_cent_residual(db, *, actor, apply=False):
    """Explain only a rounded cash residual, without granting accounting approval."""
    from app.models import StockPaperBrokerActivity, StockPaperLedgerEvent, StockPaperOrder
    from app.services.alpaca_activity_v2 import reconcile_baseline
    from app.services.alpaca_cash_precision import diagnose
    from app.services.stock_paper_ledger import active_paper_account, _event, ACCOUNTING_RESIDUAL_REVIEW_REASON, PROBE_HALT

    preview = adopt_cent_policy(db, actor=actor)
    account = active_paper_account(db, for_update=True)
    if account.cash_policy != CENT:
        raise ValueError("The explicit cent policy must already be adopted")
    if account.status != "halted" or account.halt_reason not in {
        ACCOUNTING_RESIDUAL_REVIEW_REASON, MONETARY_REVIEW_REASON, PROBE_HALT,
    }:
        raise ValueError("Only an isolated historical monetary-review halt can be reviewed")
    event = db.get(StockPaperLedgerEvent, preview["reconciliation_event_id"])
    report = event.payload
    failure = db.query(StockPaperLedgerEvent).filter(
        StockPaperLedgerEvent.account_id == account.id,
        StockPaperLedgerEvent.id > event.id,
        StockPaperLedgerEvent.status.in_(["unavailable", "drift"]),
    ).first()
    if failure:
        raise ValueError("A newer reconciliation failure invalidates monetary review")
    rows = db.query(StockPaperBrokerActivity).filter_by(account_id=account.id).all()
    orders = db.query(StockPaperOrder).filter_by(account_id=account.id).all()
    if any(order.uncertain_submission for order in orders):
        raise ValueError("Uncertain order submissions require separate review")
    raw = [row.raw_payload for row in rows]
    replay = reconcile_baseline(account.activity_baseline, raw, account.cash, {},
                                previously_seen_ids={row.broker_activity_id for row in rows})
    diagnostic = diagnose(account.activity_baseline, raw,
                          [order.raw_payload for order in orders], account.cash, {},
                          currency=account.currency, broker=account.broker)
    candidate = apply_policy(replay, diagnostic, CENT, broker=account.broker, currency=account.currency)
    if (candidate["status"] != "matched" or diagnostic.get("status") != "consistent"
            or any(candidate[key] != report.get(key) for key in candidate)
            or diagnostic != report.get("cash_precision_diagnostic")):
        raise ValueError("Independent journal replay does not match fresh broker evidence")
    # Monetary evidence cannot discharge the independent probe reservation.
    from app.services.paper_probe_review import pending_probe_reservation
    reservation = pending_probe_reservation(db, account)
    preserved_reason = PROBE_HALT if reservation or account.halt_reason == PROBE_HALT else MONETARY_REVIEW_REASON
    result = {**preview, "apply": apply, "scope": "monetary_residual_only",
              "unrounded_cash_residual": candidate["unrounded_cash_residual"],
              "cash_rounding_adjustment": candidate["cash_rounding_adjustment"],
              "residual_was_unexplained": account.unexplained_residual,
              "accounting_approved": False, "recovery_review_cleared": False,
              "preserved_halt_reason": preserved_reason,
              "preserved_probe_reservation_id": reservation.id if reservation else None}
    if apply and (account.unexplained_residual or account.halt_reason != preserved_reason):
        db.info["stock_paper_actor"] = actor.strip()
        account.unexplained_residual = False
        account.halt_reason = preserved_reason
        _event(db, account, "monetary_residual_review", "resolved", preserved_reason, result)
    return result
