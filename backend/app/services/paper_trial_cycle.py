"""One bounded operator-approved, nonqualifying paper decision cycle.

No registration, approval, kill-switch clearing or live execution is performed.
Existing ambiguous submissions are reconciled, never resubmitted.
"""
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from datetime import datetime, timezone

from sqlalchemy import select

from app.models.execution import PaperExecutionAccount, PaperOrderIntent, PaperTrialApproval
from app.models.shadow import ShadowDecision
from app.services.paper_execution import execution_transaction, reserve_intent, PaperExecutionError
from app.services.paper_recovery import run_recovery_cycle
from app.services.freqtrade_dispatch import dispatch_intent


def _result(status, reason, **extra):
    return {"status": status, "reason": reason, "mode": "dry_run",
        "eligible_for_qualification": False, "live_authorized": False, **extra}


def run_paper_trial_cycle(factory, client, approval_id):
    """Recover, reserve at most one latest decision, then dispatch at most once.

    A matching never-submitted reservation may resume after a process restart;
    submitting/unknown/submitted orders may only proceed through recovery.
    Every write uses the serialized ledger context and network calls occur
    outside it. Operator approvals and arming must already exist.
    """
    if type(approval_id) is not int or approval_id <= 0:
        raise PaperExecutionError("An explicit paper-trial approval id is required")
    recovery = run_recovery_cycle(factory, client)
    if recovery.get("status") != "success" or recovery.get("kill_switch") is not False:
        return _result("blocked", "recovery_or_kill_switch_requires_operator", recovery=recovery)
    with factory() as db, execution_transaction(db):
        account = db.get(PaperExecutionAccount, 1)
        approval = db.get(PaperTrialApproval, approval_id)
        if not account or account.kill_switch:
            return _result("blocked", "paper_account_not_armed")
        if not approval or not approval.active or not approval.nonqualifying:
            return _result("blocked", "active_nonqualifying_operator_approval_required")
        decision = db.scalar(select(ShadowDecision).where(ShadowDecision.binding_id == approval.binding_id)
            .order_by(ShadowDecision.observed_at.desc(), ShadowDecision.id.desc()).limit(1))
        if not decision or decision.backfilled:
            return _result("waiting", "no_current_forward_decision")
        observed = decision.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
        if not 0 <= age <= 900:
            return _result("waiting", "no_current_forward_decision")
        outstanding = db.scalar(select(PaperOrderIntent).where(PaperOrderIntent.status.in_(
            ("approved", "submitting", "unknown", "submitted"))).order_by(PaperOrderIntent.id))
        consumed = db.scalar(select(PaperOrderIntent).where(PaperOrderIntent.decision_id == decision.id))
        expected_client_id = f"trial-{approval_id}-decision-{decision.id}"
        if outstanding:
            if (outstanding.status != "approved" or outstanding.approval_id != approval_id
                or outstanding.decision_id != decision.id or outstanding.client_order_id != expected_client_id):
                return _result("waiting", "outstanding_intent_requires_reconciliation_or_abandonment",
                    intent_id=outstanding.id)
            # mark_submitting revalidates the current decision and approval before
            # the only POST. Never reconstruct or mutate this persisted payload.
            intent_id = outstanding.id
        elif consumed:
            return _result("waiting", "decision_already_consumed", intent_id=consumed.id)
        elif decision.intended_action == "hold":
            return _result("waiting", "model_hold", decision_id=decision.id)
        elif decision.intended_action == "buy" and account.quantity > 0:
            return _result("waiting", "existing_position_no_pyramiding", decision_id=decision.id)
        elif decision.intended_action == "sell" and account.quantity == 0:
            return _result("waiting", "no_position_no_shorting", decision_id=decision.id)
        else:
            side = decision.intended_action
            if side not in ("buy", "sell"):
                return _result("blocked", "unsupported_decision_action")
            reference = Decimal(str(decision.reference_price))
            if not reference.is_finite() or reference <= 0:
                return _result("blocked", "invalid_decision_price")
            limit = (reference * (Decimal("1.001") if side == "buy" else Decimal("0.999"))).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP)
            if limit <= 0:
                return _result("blocked", "invalid_limit_price")
            if side == "buy":
                budget = min(approval.max_notional, approval.max_exposure,
                    (account.cash - account.reserved_cash) / (1 + approval.fee_rate))
                quantity = (budget / limit).quantize(Decimal("0.00000001"), rounding=ROUND_FLOOR)
            else:
                # Entry limits must not strand profitable position dust. An exit
                # reduces risk and is bounded by owned unreserved BTC, not entry
                # notional at an older price.
                quantity = (account.quantity - account.reserved_quantity).quantize(
                    Decimal("0.00000001"), rounding=ROUND_FLOOR)
            if quantity <= 0:
                return _result("waiting", "insufficient_available_balance")
            intent = reserve_intent(db, decision_id=decision.id, approval_id=approval_id,
                side=side, quantity=quantity, limit_price=limit, client_order_id=expected_client_id)
            intent_id = intent.id
    # The dispatch gateway owns its durable submitting transition and never
    # retries a POST. Exceptions remain visible to the supervisor/recovery loop.
    execution = dispatch_intent(factory, client, intent_id)
    return _result("processed", "latest_approved_paper_decision", intent_id=intent_id, execution=execution)
