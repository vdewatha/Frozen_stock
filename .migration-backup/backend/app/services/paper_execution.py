"""Serialized, paper-only ledger with explicit nonqualifying operator trials.

All mutations require ``execution_transaction`` on a fresh Session. The context
commits exactly once, or rolls back entirely. Never hold it across network calls:
reserve -> commit; submitting -> commit; network; submission/fills -> commit.
An interrupted submitting request is ambiguous and MUST NOT be retried.
"""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_EVEN
import hashlib
import json

from sqlalchemy import select, text

from app.models.execution import PaperExecutionAccount, PaperTrialApproval, PaperOrderIntent, PaperExecutionFill, PaperExternalFill


class PaperExecutionError(ValueError):
    pass


def _money(value, *, reserve=False):
    return value.quantize(Decimal("0.00000001"), rounding=ROUND_CEILING if reserve else ROUND_HALF_EVEN)


def _policy_hash(binding_id, run_id, spec_hash, maximum, exposure, rate):
    policy = {"version": 2, "binding_id": binding_id, "model_run_id": run_id,
        "binding_hash": spec_hash, "max_notional": format(maximum.normalize(), "f"),
        "max_exposure": format(exposure.normalize(), "f"),
        "fee_rate": format(rate.normalize(), "f"), "nonqualifying": True,
        "max_decision_age_seconds": 900,
        "max_notional_applies_to": "entries_only", "exit_policy": "owned_inventory_risk_reduction"}
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def _decimal(value, *, positive=False):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or (positive and number == 0):
            raise ValueError()
        if number != number.quantize(Decimal("0.00000001")) or number > Decimal("1000000000"):
            raise ValueError()
        return number
    except (ValueError, InvalidOperation):
        raise PaperExecutionError("Finite nonnegative amounts with at most 8 decimal places required") from None


@contextmanager
def execution_transaction(db):
    if db.in_transaction():
        raise PaperExecutionError("Use a fresh session for the serialized execution transaction")
    try:
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        elif db.get_bind().dialect.name == "postgresql":
            db.begin()
            # Transaction-scoped advisory lock also serializes initial creation.
            db.execute(text("SELECT pg_advisory_xact_lock(724191032)"))
        else:
            raise PaperExecutionError("Unsupported ledger database")
        db.info["paper_execution_locked"] = True
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.info.pop("paper_execution_locked", None)


def _locked(db):
    if not db.info.get("paper_execution_locked") or not db.in_transaction():
        raise PaperExecutionError("execution_transaction is required")


def _account(db):
    _locked(db)
    # SessionLocal disables autoflush: preserve changes made earlier in this
    # same locked transaction before refreshing cross-process account state.
    db.flush()
    account = db.get(PaperExecutionAccount, 1, populate_existing=True)
    if account is None:
        raise PaperExecutionError("Paper account not initialized")
    return account


def create_account(db, starting_cash):
    _locked(db)
    starting_cash = _decimal(starting_cash, positive=True)
    if db.get(PaperExecutionAccount, 1):
        raise PaperExecutionError("Paper account already exists; balances cannot be reset")
    account = PaperExecutionAccount(id=1, starting_cash=starting_cash, cash=starting_cash,
        reserved_cash=0, quantity=0, reserved_quantity=0, kill_switch=True)
    db.add(account)
    db.flush()
    return account


def audit_account(db):
    """Recompute ledger identities from fills and outstanding reservations."""
    account = _account(db)
    cash, quantity = account.starting_cash, Decimal(0)
    for fill, side in db.execute(select(PaperExecutionFill, PaperOrderIntent.side)
        .join(PaperOrderIntent, PaperExecutionFill.intent_id == PaperOrderIntent.id)):
        notional = _money(fill.quantity * fill.price)
        cash += -notional - fill.fee if side == "buy" else notional - fill.fee
        quantity += fill.quantity if side == "buy" else -fill.quantity
    for fill in db.scalars(select(PaperExternalFill)):
        cash += fill.cost - fill.fee
        quantity -= fill.quantity
    intents = db.scalars(select(PaperOrderIntent)).all()
    reserved_cash = sum((o.reserved_cash for o in intents), Decimal(0))
    reserved_quantity = sum((o.reserved_quantity for o in intents), Decimal(0))
    if ((cash, quantity, reserved_cash, reserved_quantity) !=
        (account.cash, account.quantity, account.reserved_cash, account.reserved_quantity)
        or min(cash, quantity, reserved_cash, reserved_quantity) < 0
        or reserved_cash > cash or reserved_quantity > quantity):
        raise PaperExecutionError("Paper ledger accounting identity mismatch")
    return {"cash": str(cash), "quantity": str(quantity),
        "available_cash": str(cash - reserved_cash),
        "available_quantity": str(quantity - reserved_quantity),
        "accounting_consistent": True, "mode": "paper", "eligible_for_qualification": False}


def set_kill_switch(db, enabled: bool):
    if type(enabled) is not bool:
        raise PaperExecutionError("Kill switch must be boolean")
    _account(db).kill_switch = enabled


def _validate_binding(db, binding):
    from app.models.models import ResearchModelRun
    from app.services.shadow_pipeline import INSTRUMENT, validate_spec
    if not binding or binding.instrument != INSTRUMENT:
        raise PaperExecutionError("Only a persisted Kraken BTC/USD binding can be approved")
    run = db.scalar(select(ResearchModelRun).where(ResearchModelRun.run_id == binding.run_id))
    try:
        if (run is None or validate_spec(run, binding.spec) != binding.spec_sha256
            or binding.manifest_sha256 != run.manifest_sha256
            or binding.timeframe_minutes != binding.spec["timeframe_minutes"]):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise PaperExecutionError("Persisted model specification integrity mismatch") from None


def approve_trial(db, *, binding_id, actor, max_notional, max_exposure, fee_rate):
    from app.models.shadow import ShadowModelBinding
    _account(db)
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 128:
        raise PaperExecutionError("Explicit operator actor is required")
    binding = db.get(ShadowModelBinding, binding_id)
    _validate_binding(db, binding)
    maximum, exposure, rate = (_decimal(max_notional, positive=True),
        _decimal(max_exposure, positive=True), _decimal(fee_rate))
    if maximum > exposure or rate > Decimal("0.05"):
        raise PaperExecutionError("Invalid notional/exposure/fee policy")
    approval = PaperTrialApproval(binding_id=binding_id, model_run_id=binding.run_id,
        binding_hash=binding.spec_sha256, policy_hash=_policy_hash(binding_id,
        binding.run_id, binding.spec_sha256, maximum, exposure, rate), actor=actor.strip(), max_notional=maximum,
        max_exposure=exposure, fee_rate=rate, nonqualifying=True, active=True)
    db.add(approval)
    db.flush()
    return approval


def _naive_utc(now):
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if now.tzinfo is None:
        raise PaperExecutionError("now must be timezone aware")
    return now.astimezone(timezone.utc).replace(tzinfo=None)


def _validate_decision(db, decision_id, approval_id, side, now):
    from app.models.shadow import ShadowModelBinding, ShadowDecision
    approval = db.get(PaperTrialApproval, approval_id)
    decision = db.get(ShadowDecision, decision_id)
    binding = db.get(ShadowModelBinding, decision.binding_id) if decision else None
    if not approval or not approval.active or not approval.nonqualifying or not binding:
        raise PaperExecutionError("Explicit active nonqualifying paper-trial approval required")
    _validate_binding(db, binding)
    if (binding.id, binding.run_id, binding.spec_sha256) != (approval.binding_id, approval.model_run_id, approval.binding_hash):
        raise PaperExecutionError("Model binding does not match operator approval")
    if approval.policy_hash != _policy_hash(approval.binding_id, approval.model_run_id,
        approval.binding_hash, approval.max_notional, approval.max_exposure, approval.fee_rate):
        raise PaperExecutionError("Approved risk policy integrity mismatch")
    observed = decision.observed_at
    if observed.tzinfo is not None:
        observed = observed.astimezone(timezone.utc).replace(tzinfo=None)
    age = _naive_utc(now) - observed
    latest = db.scalar(select(ShadowDecision.id).where(ShadowDecision.binding_id == binding.id)
        .order_by(ShadowDecision.observed_at.desc(), ShadowDecision.id.desc()).limit(1))
    if (decision.backfilled or decision.intended_action != side
        or decision.spec_sha256 != approval.binding_hash or latest != decision.id
        or age < timedelta(0) or age > timedelta(seconds=900)):
        raise PaperExecutionError("Decision is stale, backfilled, mismatched, or not the latest actionable decision")
    return approval, decision


def reserve_intent(db, *, decision_id, approval_id, side, quantity, limit_price,
                   client_order_id, now=None):
    account = _account(db)
    audit_account(db)
    qty, price = _decimal(quantity, positive=True), _decimal(limit_price, positive=True)
    if side not in ("buy", "sell") or not isinstance(client_order_id, str) or not 1 <= len(client_order_id) <= 128:
        raise PaperExecutionError("Invalid side/client order id")
    existing = db.scalar(select(PaperOrderIntent).where(PaperOrderIntent.client_order_id == client_order_id))
    if existing:
        if (existing.decision_id, existing.approval_id, existing.side, existing.quantity, existing.limit_price) != (decision_id, approval_id, side, qty, price):
            raise PaperExecutionError("Client order id conflicts with immutable intent")
        return existing
    if db.scalar(select(PaperOrderIntent.id).where(PaperOrderIntent.decision_id == decision_id, PaperOrderIntent.side == side)):
        raise PaperExecutionError("Decision already reserved under another client id")
    if account.kill_switch:
        raise PaperExecutionError("Paper execution kill switch is engaged")
    # One in-flight order globally avoids ambiguous exposure and competing exits.
    if db.scalar(select(PaperOrderIntent.id).where(PaperOrderIntent.status.in_(("approved", "submitting", "unknown", "submitted")))):
        raise PaperExecutionError("An outstanding or ambiguous order requires reconciliation")
    approval, decision = _validate_decision(db, decision_id, approval_id, side, now)
    reference = _decimal(decision.reference_price, positive=True)
    if abs(price / reference - 1) > Decimal("0.01"):
        raise PaperExecutionError("Limit price is more than 1% from the decision reference")
    notional = qty * price
    if notional < Decimal("0.00000001") or (side == "buy" and notional > approval.max_notional):
        raise PaperExecutionError("Order exceeds approved notional")
    reserved_cash, reserved_quantity = Decimal(0), Decimal(0)
    if side == "buy":
        if (account.quantity + qty) * price > approval.max_exposure:
            raise PaperExecutionError("Order exceeds approved total exposure")
        reserved_cash = _money(notional * (1 + approval.fee_rate), reserve=True)
        if reserved_cash > account.cash - account.reserved_cash:
            raise PaperExecutionError("Insufficient available paper cash")
    else:
        reserved_quantity = qty
        if qty > account.quantity - account.reserved_quantity:
            raise PaperExecutionError("Insufficient available BTC; shorts are forbidden")
    intent = PaperOrderIntent(client_order_id=client_order_id, decision_id=decision_id,
        approval_id=approval_id, side=side, quantity=qty, limit_price=price,
        reserved_cash=reserved_cash, reserved_quantity=reserved_quantity, filled_quantity=0,
        status="approved")
    account.reserved_cash += reserved_cash
    account.reserved_quantity += reserved_quantity
    db.add(intent)
    db.flush()
    return intent


def _intent(db, intent_id):
    _locked(db)
    intent = db.get(PaperOrderIntent, intent_id)
    if not intent:
        raise PaperExecutionError("Unknown intent")
    return intent


def mark_submitting(db, intent_id, *, provider_context=None, now=None):
    account, intent = _account(db), _intent(db, intent_id)
    audit_account(db)
    if account.kill_switch or intent.status != "approved":
        raise PaperExecutionError("Intent cannot be submitted")
    _validate_decision(db, intent.decision_id, intent.approval_id, intent.side, now)
    if provider_context is not None:
        if not isinstance(provider_context, dict):
            raise PaperExecutionError("Provider context must be an object")
        encoded = json.dumps(provider_context, sort_keys=True, allow_nan=False)
        if len(encoded) > 8192:
            raise PaperExecutionError("Provider context too large")
        intent.provider_context = json.loads(encoded)
    intent.status = "submitting"
    return intent


def abandon_unsubmitted(db, intent_id):
    """Release a local reservation only when no remote submission ever began."""
    account, intent = _account(db), _intent(db, intent_id)
    if intent.status != "approved" or intent.provider_order_id or intent.provider_context:
        raise PaperExecutionError("Only never-submitted reservations can be abandoned locally")
    account.reserved_cash -= intent.reserved_cash
    account.reserved_quantity -= intent.reserved_quantity
    intent.reserved_cash = Decimal(0)
    intent.reserved_quantity = Decimal(0)
    intent.status = "abandoned"
    return intent


def record_submission(db, intent_id, *, provider_order_id=None, outcome="submitted"):
    intent = _intent(db, intent_id)
    if outcome not in ("submitted", "unknown", "rejected"):
        raise PaperExecutionError("Invalid submission outcome")
    if intent.status not in ("submitting", "unknown"):
        raise PaperExecutionError("Intent is not awaiting submission resolution")
    if outcome == "submitted":
        if not isinstance(provider_order_id, str) or not 1 <= len(provider_order_id) <= 128:
            raise PaperExecutionError("A provider order id is required")
        if intent.provider_order_id and intent.provider_order_id != provider_order_id:
            raise PaperExecutionError("Provider order identity is immutable")
        intent.provider_order_id = provider_order_id
    if outcome == "rejected":
        return reconcile(db, intent_id, final_status="rejected")
    intent.status = outcome
    return intent


def apply_fill(db, intent_id, *, provider_fill_id, quantity, price, fee):
    account, intent = _account(db), _intent(db, intent_id)
    qty, price, fee = _decimal(quantity, positive=True), _decimal(price, positive=True), _decimal(fee)
    if not isinstance(provider_fill_id, str) or not 1 <= len(provider_fill_id) <= 128:
        raise PaperExecutionError("Provider fill id required")
    existing = db.scalar(select(PaperExecutionFill).where(PaperExecutionFill.provider_fill_id == provider_fill_id))
    if existing:
        if (existing.intent_id, existing.quantity, existing.price, existing.fee) != (intent_id, qty, price, fee):
            raise PaperExecutionError("Conflicting duplicate fill")
        return existing
    if intent.status != "submitted" or not intent.provider_order_id:
        raise PaperExecutionError("Fills require a confirmed submitted order")
    if intent.filled_quantity + qty > intent.quantity:
        raise PaperExecutionError("Fill exceeds reserved order quantity")
    notional = _money(qty * price)
    approval = db.get(PaperTrialApproval, intent.approval_id)
    if notional <= 0 or fee > _money(notional * approval.fee_rate, reserve=True):
        raise PaperExecutionError("Fill fees exceed approved reserve; manual reconciliation required")
    if intent.side == "buy":
        if price > intent.limit_price or notional + fee > intent.reserved_cash or notional + fee > account.cash:
            raise PaperExecutionError("Buy fill exceeds limit/reserved cash")
        account.cash -= notional + fee
        account.quantity += qty
        account.reserved_cash -= notional + fee
        intent.reserved_cash -= notional + fee
    else:
        if price < intent.limit_price or qty > intent.reserved_quantity or qty > account.quantity:
            raise PaperExecutionError("Sell fill exceeds limit/reserved inventory")
        account.cash += notional - fee
        account.quantity -= qty
        account.reserved_quantity -= qty
        intent.reserved_quantity -= qty
    intent.filled_quantity += qty
    fill = PaperExecutionFill(provider_fill_id=provider_fill_id, intent_id=intent_id,
        quantity=qty, price=price, fee=fee)
    db.add(fill)
    db.flush()
    return fill


def reconcile(db, intent_id, *, final_status):
    account, intent = _account(db), _intent(db, intent_id)
    if final_status not in ("filled", "cancelled", "rejected"):
        raise PaperExecutionError("Terminal provider evidence required")
    if intent.status in ("reconciled", "rejected"):
        raise PaperExecutionError("Intent already terminal")
    if ((final_status in ("filled", "cancelled") and intent.status != "submitted")
        or (final_status == "rejected" and intent.status not in ("submitting", "unknown", "submitted"))):
        raise PaperExecutionError("Terminal status requires a matching submission state")
    if final_status == "filled" and intent.filled_quantity != intent.quantity:
        raise PaperExecutionError("Not all reported fills have been accounted")
    if final_status == "rejected" and intent.filled_quantity:
        raise PaperExecutionError("Filled orders cannot be rejected")
    account.reserved_cash -= intent.reserved_cash
    account.reserved_quantity -= intent.reserved_quantity
    intent.reserved_cash = Decimal(0)
    intent.reserved_quantity = Decimal(0)
    intent.status = "rejected" if final_status == "rejected" else "reconciled"
    return intent
