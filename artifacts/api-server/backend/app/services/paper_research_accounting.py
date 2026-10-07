"""Observed paper accounting with explicit cost uncertainty, not execution approval."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json

from app.core.config import settings
from app.models import StockPaperBrokerActivity, StockPaperFill, StockPaperLedgerEvent, StockPaperOrder, StockPaperPosition
from app.services import alpaca_activity_v2 as activity
from app.services.alpaca_cash_precision import diagnose
from app.services.paper_cash_policy import EXACT, CENT, MONETARY_REVIEW_REASON, apply_policy
from app.services.paper_cost_sensitivity import evaluate
from app.services.alpaca_paper_cost_contract import assess as assess_cost_contract

VERSION = "paper-research-accounting-v1"
TRANSPORT_HALT = "Alpaca paper broker is unavailable or returned invalid data"


def _same_evidence_value(key, persisted, current):
    """Treat equivalent persisted Decimal spellings as the same evidence."""
    if key.endswith("_residual") or key in {"gross_reported_cash_delta", "reported_fill_commissions"}:
        try:
            return Decimal(str(persisted)) == Decimal(str(current))
        except (InvalidOperation, TypeError, ValueError):
            return False
    return persisted == current


def assess(db, account, *, now=None):
    return _assess(db, account, now=now)


def _assess(db, account, *, now=None, transport_review=False, probe_review=False):
    """Verify current closed-inventory evidence without mutating gates or fees."""
    now = now or datetime.now(timezone.utc)
    result = {"version": VERSION, "scope": "closed_inventory_paper_research",
              "status": "blocked", "accounting_observation_ready": False,
              "costs_verified": False, "launch_authorized": False, "live_authorized": False,
              "remaining_execution_requirements": ["paper_venue_activation", "recovery_revalidation",
                                                    "current_execution_data", "approved_strategy_and_risk_limits"]}
    if transport_review:
        result["scope"] = "transport_recovery_observation_only"
    if probe_review:
        result["scope"] = "probe_completion_observation_only"
    def require(condition, reason):
        if not condition:
            raise ValueError(reason)
    def utc(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    try:
        require(now.tzinfo is not None, "A timezone-aware evaluation clock is required")
        require(not settings.allow_live_trading and settings.active_paper_broker == "alpaca_paper",
                "Only a live-disabled Alpaca paper environment is supported")
        require(account is not None and account.broker == "alpaca_paper" and account.currency == "USD",
                "An initialized USD Alpaca paper account is required")
        require(account.activity_contract == activity.VERSION and account.cash_policy in {EXACT, CENT},
                "A supported versioned journal and cash policy are required")
        require(not account.unexplained_residual and not account.reconciliation_required,
                "Unresolved accounting evidence prevents paper research")
        from app.services.stock_paper_ledger import PROBE_HALT
        require(not (transport_review and probe_review), "Review scopes cannot be combined")
        accepted_halt = PROBE_HALT if probe_review else TRANSPORT_HALT if transport_review else MONETARY_REVIEW_REASON
        # A clean reread may clear only the transient provider halt before the
        # operator review runs.  The review itself still requires the prior
        # unavailable event and two fresh matching reconciliations, so allowing
        # the reconciled/clear state here cannot authorize execution.
        transport_state_ok = (
            transport_review
            and (
                (account.status == "halted" and account.halt_reason == TRANSPORT_HALT)
                or (account.status == "reconciled" and account.halt_reason is None)
            )
        )
        monetary_review_state_ok = (
            not transport_review
            and not probe_review
            and account.status == "halted"
            and account.halt_reason == MONETARY_REVIEW_REASON
        )
        latest_transport_failure = db.query(StockPaperLedgerEvent).filter_by(
            account_id=account.id, event_type="reconcile", status="unavailable",
            reason=TRANSPORT_HALT,
        ).order_by(StockPaperLedgerEvent.id.desc()).first()
        latest_transport_review = db.query(StockPaperLedgerEvent).filter_by(
            account_id=account.id, event_type="paper_transport_recovery_review",
        ).order_by(StockPaperLedgerEvent.id.desc()).first()
        transport_recovery_pending = bool(
            latest_transport_failure is not None
            and (latest_transport_review is None
                 or latest_transport_review.id < latest_transport_failure.id)
        )
        require((not transport_review and not probe_review and account.status == "reconciled")
                and not transport_recovery_pending
                or monetary_review_state_ok or transport_state_ok
                or (account.status == "halted" and account.halt_reason == accepted_halt),
                "An unrelated account halt requires separate review")
        require(account.last_reconciled_at is not None and 0 <= (now-utc(account.last_reconciled_at)).total_seconds() <= 120,
                "A fresh broker reconciliation is required")
        raw_account = account.raw_payload
        require(raw_account.get("id") == account.broker_account_id and raw_account.get("currency") == "USD"
                and raw_account.get("status") == "ACTIVE", "Broker identity, currency and account status must match")
        require(all(raw_account.get(key) is False for key in ("trading_blocked", "account_blocked", "trade_suspended_by_user")),
                "Explicit broker permission evidence is required")
        require(activity.amount(raw_account.get("cash")) == account.cash and activity.amount(raw_account.get("equity")) == account.equity,
                "Broker balances differ from the ledger")
        require(not db.query(StockPaperPosition).filter_by(account_id=account.id).count(),
                "This entry contract requires flat inventory")
        orders = db.query(StockPaperOrder).filter_by(account_id=account.id).all()
        require(all(not o.uncertain_submission and o.status in {"filled", "canceled", "expired", "rejected", "replaced"} for o in orders),
                "Pending or uncertain orders require reconciliation")
        for order in orders:
            payload = order.raw_payload or {}
            require(payload.get("id") == order.broker_order_id and payload.get("status") == order.status
                    and payload.get("symbol") == order.symbol and payload.get("side") == order.side
                    and activity.amount(payload.get("qty") or payload.get("filled_qty")) == order.quantity,
                    "Persisted order fields differ from broker evidence")
        rows = db.query(StockPaperBrokerActivity).filter_by(account_id=account.id).all()
        raw = [row.raw_payload for row in rows]
        report = activity.reconcile_baseline(account.activity_baseline, raw, account.cash, {},
                    previously_seen_ids={row.broker_activity_id for row in rows})
        diagnostic = diagnose(account.activity_baseline, raw, [o.raw_payload for o in orders], account.cash, {},
                              currency=account.currency, broker=account.broker)
        require(diagnostic.get("status") == "consistent", "Orders and activities need unambiguous monetary evidence")
        report = apply_policy(report, diagnostic, account.cash_policy, broker=account.broker, currency=account.currency)
        require(report["status"] == "matched", "Cash and inventory must reconcile")
        latest = db.query(StockPaperLedgerEvent).filter_by(account_id=account.id, event_type="activity_reconciliation").order_by(StockPaperLedgerEvent.id.desc()).first()
        require(latest is not None and 0 <= (now-utc(latest.created_at)).total_seconds() <= 120,
                "Fresh persisted reconciliation evidence is required")
        require(all(_same_evidence_value(key, (latest.payload or {}).get(key), value) for key, value in report.items()),
                "Independent replay differs from the persisted broker reconciliation")
        fills = db.query(StockPaperFill).filter_by(account_id=account.id).all()
        by_id = {fill.broker_activity_id: fill for fill in fills}
        by_order = {order.broker_order_id: order for order in orders}
        executions = [row for row in raw if row.get("activity_type") == "FILL"]
        require(set(by_id) == {row["id"] for row in executions}, "Execution journal and fill ledger differ")
        for row in executions:
            fill, order = by_id[row["id"]], by_order.get(row["order_id"])
            require(order is not None and fill.order_id == order.id and fill.broker_order_id == order.broker_order_id
                    and fill.symbol == order.symbol == row["symbol"] and fill.side == order.side == row["side"]
                    and fill.quantity == activity.amount(row["qty"]) and fill.price == activity.amount(row["price"]),
                    "Execution identity, order link or precision is inconsistent")
            commission = activity.amount(row["commission"]) if row.get("commission") is not None else None
            require(fill.fee == commission and fill.cost_known == (commission is not None),
                    "Persisted fill costs differ from reported evidence")
        costs = evaluate(account.activity_baseline, raw, {}, currency=account.currency)
        cost_contract = assess_cost_contract(
            provider=account.broker,
            activity_contract=account.activity_contract,
            activities=raw,
        )
        require(costs["status"] == "research_only" and costs["journal_sha256"] == report["journal_sha256"],
                "A closed-inventory modeled-cost report is required")
        result.update(status="observed_ready", accounting_observation_ready=True,
            reason="Observed broker accounting passes; modeled costs are not verified fees or trading approval",
            account_id_sha256=hashlib.sha256(account.broker_account_id.encode()).hexdigest(),
            journal_sha256=report["journal_sha256"], baseline_journal_sha256=report["baseline_journal_sha256"],
            cash_policy=account.cash_policy, reconciliation_event_id=latest.id,
            observed_at=utc(account.last_reconciled_at).isoformat(),
            cost_policy={"assumptions": costs["assumptions"], "assumptions_sha256": costs["assumptions_sha256"],
                         "fills_without_reported_commission": costs["fills_without_reported_commission"],
                         "missing_fees_treated_as_zero": False,
                         "provider_contract": cost_contract},
            activity_count=len(rows), execution_count=len(fills), order_count=len(orders))
    except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as exc:
        result["reason"] = str(exc) if isinstance(exc, ValueError) else "Invalid paper accounting evidence"
    result["report_sha256"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result
