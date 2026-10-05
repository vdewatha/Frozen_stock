"""Observed equity changes, separate from qualified strategy performance."""
from datetime import datetime, timezone
from decimal import Decimal

from app.models import StockPaperBrokerActivity, StockPaperEquitySnapshot, StockPaperLedgerEvent, StockPaperPosition, StockPaperOrder
from app.services import alpaca_activity_v2 as activity
from app.services.alpaca_cash_precision import diagnose
from app.services.paper_cash_policy import EXACT, CENT, MONETARY_REVIEW_REASON, apply_policy


def _unavailable(reason):
    return {"status": "unavailable", "reason": reason, "qualifying": False,
            "scope": "observed_since_initialization", "net_change": None}


def calculate_observed_change(baseline, activities, *, opening_equity, closing_equity,
                              closing_cash, closing_positions, cash_policy=EXACT,
                              orders=None, broker="alpaca_paper", currency="USD"):
    """Subtract external funding from equity change; do not deduct fees twice.

    This is a dollar change between observed snapshots, not a time-weighted
    return, account-inception P/L, or proof that the broker reported every fee.
    Date-only activities are compared by the frozen journal, not invented times.
    """
    try:
        before = baseline["activities"]
        replay = activity.reconcile_baseline(
            baseline, activities, closing_cash, closing_positions,
            previously_seen_ids={row["id"] for row in before},
        )
        diagnostic = diagnose(baseline, activities, orders, closing_cash, closing_positions,
                              currency=currency, broker=broker) if cash_policy != EXACT else None
        replay = apply_policy(replay, diagnostic, cash_policy, broker=broker, currency=currency)
        if replay["status"] != "matched":
            return _unavailable("Cash or inventory does not match the observed baseline")
        old = {row["id"]: activity.normalize(row) for row in before}
        new = {row["id"]: activity.normalize(row) for row in activities}
        funding = Decimal(0)
        reported_fees = Decimal(0)
        unknown_fill_costs = 0
        for identifier, event in new.items():
            prior = old.get(identifier)
            if prior and prior["type"] != event["type"]:
                return _unavailable("Activity classification changed across observations")
            cash_delta = activity.amount(event["gross_cash_delta"]) - (
                activity.amount(prior["gross_cash_delta"]) if prior else Decimal(0)
            )
            if event["type"] in {"CSD", "CSW"}:
                funding += cash_delta
            elif event["type"] == "JNLC" and cash_delta:
                return _unavailable("Cash journal requires external-flow classification")
            elif event["type"] in {"FEE", "CFEE"}:
                reported_fees -= cash_delta
            elif event["type"] == "FILL":
                commission = event["reported_fill_commission"]
                old_commission = prior["reported_fill_commission"] if prior else None
                reported_fees += (activity.amount(commission) if commission is not None else Decimal(0))
                reported_fees -= (activity.amount(old_commission) if old_commission is not None else Decimal(0))
                if prior is None and commission is None:
                    unknown_fill_costs += 1
        start, end = activity.amount(opening_equity), activity.amount(closing_equity)
        return {
            "status": "provisional", "qualifying": False,
            "scope": "observed_since_initialization",
            "reason": "Observed broker equity less external funding; cost completeness is unverified",
            "opening_equity": str(start), "closing_equity": str(end),
            "external_net_funding": str(funding), "net_change": str(end - start - funding),
            "reported_fee_expense": str(reported_fees),
            "new_fills_without_commission": unknown_fill_costs,
            "costs_complete": False,
            "journal_sha256": replay["journal_sha256"],
        }
    except (KeyError, TypeError, ValueError):
        return _unavailable("Activity baseline or monetary evidence is invalid")


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def observed_paper_performance(db, account):
    if account.activity_contract != activity.VERSION or not account.activity_baseline:
        return _unavailable("A frozen Alpaca activity baseline is required")
    reviewed_halt = False
    if account.status == "halted" and account.halt_reason == MONETARY_REVIEW_REASON and account.cash_policy == CENT:
        review = db.query(StockPaperLedgerEvent).filter_by(
            account_id=account.id, event_type="monetary_residual_review",
        ).order_by(StockPaperLedgerEvent.id.desc()).first()
        proof = (review.payload or {}) if review else {}
        reviewed_halt = bool(review and review.status == "resolved"
            and proof.get("scope") == "monetary_residual_only"
            and proof.get("policy") == account.cash_policy
            and proof.get("baseline_journal_sha256") == account.activity_baseline.get("journal_sha256")
            and proof.get("accounting_approved") is False
            and proof.get("launch_authorized") is False)
    if (account.status != "reconciled" and not reviewed_halt) or account.reconciliation_required or account.unexplained_residual:
        return _unavailable("The current account must reconcile without unexplained residuals")
    first = db.query(StockPaperEquitySnapshot).filter_by(account_id=account.id).order_by(
        StockPaperEquitySnapshot.observed_at, StockPaperEquitySnapshot.id,
    ).first()
    last = db.query(StockPaperEquitySnapshot).filter_by(account_id=account.id).order_by(
        StockPaperEquitySnapshot.observed_at.desc(), StockPaperEquitySnapshot.id.desc(),
    ).first()
    try:
        baseline = account.activity_baseline
        if (first is None or last is None or account.last_reconciled_at is None
                or _utc(first.observed_at) != _utc(baseline["observed_at"])
                or _utc(last.observed_at) != _utc(account.last_reconciled_at)
                or first.source != account.broker or last.source != account.broker
                or activity.amount(first.cash) != activity.amount(baseline["cash"])
                or activity.amount(last.cash) != activity.amount(account.cash)
                or activity.amount(last.equity) != activity.amount(account.equity)):
            return _unavailable("Equity snapshots do not match the reconciliation boundaries")
    except (KeyError, TypeError, ValueError, AttributeError):
        return _unavailable("Equity observation boundaries are invalid")
    # Read the full account journal, not the UI's limited fills/snapshot lists.
    rows = db.query(StockPaperBrokerActivity).filter_by(account_id=account.id).all()
    positions = db.query(StockPaperPosition).filter_by(account_id=account.id).all()
    result = calculate_observed_change(
        baseline, [row.raw_payload for row in rows], opening_equity=first.equity,
        closing_equity=last.equity, closing_cash=account.cash,
        closing_positions={row.symbol: row.quantity for row in positions},
        cash_policy=account.cash_policy, broker=account.broker, currency=account.currency,
        orders=[row.raw_payload for row in db.query(StockPaperOrder).filter_by(account_id=account.id).all()],
    )
    evidence = db.query(StockPaperLedgerEvent).filter_by(
        account_id=account.id, event_type="activity_reconciliation",
    ).order_by(StockPaperLedgerEvent.id.desc()).first()
    expected_digest = (evidence.payload or {}).get("journal_sha256") if evidence else baseline["journal_sha256"]
    if reviewed_halt and (not evidence or (evidence.payload or {}).get("status") != "matched"):
        return _unavailable("Reviewed monetary halt requires a matching reconciliation report")
    if result["status"] == "provisional" and result["journal_sha256"] != expected_digest:
        return _unavailable("Activity journal differs from the reconciled observation")
    if reviewed_halt and result["status"] == "provisional":
        result["reason"] += "; account remains halted and trading is not authorized"
    return result | {"from": _utc(first.observed_at).isoformat(),
                     "as_of": _utc(last.observed_at).isoformat(), "currency": account.currency}
