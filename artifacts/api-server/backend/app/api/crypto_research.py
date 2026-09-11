"""Authenticated research controls. No executable model uploads or live orders."""
from typing import Literal
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_db
from app.models.crypto_data import CollectionRun, CryptoCandle
from app.models.shadow import ShadowDecision, ShadowModelBinding, ShadowRunAudit
from app.models.execution import PaperExecutionAccount, PaperOrderIntent, PaperExternalFill
from app.services.crypto_collection import collect_kraken
from app.services.shadow_pipeline import register_shadow_model, run_shadow, score_shadow
from app.services import paper_execution as ledger
from app.services.audit import write_audit_log
from app.core.config import settings
from app.integrations.freqtrade import FreqtradeError
from app.integrations.freqtrade_execution import FreqtradeDryRunClient
from app.services.freqtrade_dispatch import dispatch_intent, reconcile_intent
from app.services.paper_recovery import run_recovery_cycle
from app.services.paper_performance import paper_performance

router = APIRouter(prefix="/crypto", tags=["crypto research"])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BindingBody(StrictBody):
    run_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec: dict


class AccountBody(StrictBody):
    starting_cash: str = Field(max_length=32)


class TrialBody(StrictBody):
    binding_id: int = Field(gt=0)
    max_notional: str = Field(max_length=32)
    max_exposure: str = Field(max_length=32)
    fee_rate: str = Field(max_length=32)
    acknowledge_nonqualifying_trial: Literal[True]


class IntentBody(StrictBody):
    decision_id: int = Field(gt=0)
    approval_id: int = Field(gt=0)
    side: Literal["buy", "sell"]
    quantity: str = Field(max_length=32)
    limit_price: str = Field(max_length=32)
    client_order_id: str = Field(min_length=1, max_length=128)


class ReconcileBody(StrictBody):
    trade_id: int | None = Field(default=None, gt=0)


def audit(db, request, action, entity_id=None):
    write_audit_log(db, event_type="crypto_research", action=action, status="success",
                   message="Authenticated paper/research operation", entity_id=entity_id,
                   payload={"actor": request.state.actor, "request_id": request.state.request_id})


def project(row, fields):
    return {key: str(value) if isinstance(value, Decimal) else value
            for key in fields for value in (getattr(row, key),)}


@router.get("/status")
def status(db: Session = Depends(get_db)):
    latest = db.scalar(select(CollectionRun).order_by(CollectionRun.id.desc()).limit(1))
    return {"live_trading_enabled": False,
            "candles": db.scalar(select(func.count()).select_from(CryptoCandle)),
            "bindings": db.scalar(select(func.count()).select_from(ShadowModelBinding)),
            "collection": project(latest, ("id", "status", "observed_at", "fetched_count", "inserted_count", "error_code")) if latest else None}


@router.post("/collect")
def collect(request: Request, db: Session = Depends(get_db)):
    row = collect_kraken(db)
    audit(db, request, "collect", row.id)
    db.commit()
    return project(row, ("id", "status", "fetched_count", "inserted_count", "error_code"))


@router.post("/bindings")
def bind(body: BindingBody, request: Request, db: Session = Depends(get_db)):
    try:
        row = register_shadow_model(db, body.run_id, body.spec, request.state.actor)
        audit(db, request, "bind_shadow", row.id)
        db.commit()
        return {"id": row.id, "run_id": row.run_id, "spec_sha256": row.spec_sha256}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None


@router.get("/bindings")
def bindings(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    return [project(row, ("id", "run_id", "spec_sha256", "instrument", "timeframe_minutes", "created_at"))
            for row in db.scalars(select(ShadowModelBinding).order_by(ShadowModelBinding.id.desc()).limit(limit))]


@router.post("/bindings/{binding_id}/observe")
def observe(binding_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        row = run_shadow(db, binding_id)
        scored = score_shadow(db)
        audit(db, request, "observe_shadow", binding_id)
        db.commit()  # Persist blocked observation audits too.
        return {"decision_id": row.id if row else None, "scored": scored, "eligible_for_qualification": False}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None


@router.get("/decisions")
def decisions(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    return [project(row, ("id", "binding_id", "bar_close", "observed_at", "data_sha256", "spec_sha256",
                         "probability", "reference_price", "intended_action", "backfilled", "eligible_for_qualification", "outcome"))
            for row in db.scalars(select(ShadowDecision).order_by(ShadowDecision.id.desc()).limit(limit))]


@router.get("/shadow-audits")
def shadow_audits(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    return [project(row, ("id", "binding_id", "observed_at", "status", "reason"))
            for row in db.scalars(select(ShadowRunAudit).order_by(ShadowRunAudit.id.desc()).limit(limit))]


def mutate(db, request, action, operation):
    try:
        with ledger.execution_transaction(db):
            result = operation()
            audit(db, request, action, getattr(result, "id", None))
            answer = {"id": result.id} if result is not None else {"ok": True}
        return answer
    except ledger.PaperExecutionError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/paper/account")
def initialize(body: AccountBody, request: Request, db: Session = Depends(get_db)):
    return mutate(db, request, "initialize_paper", lambda: ledger.create_account(db, body.starting_cash))


@router.post("/paper/trials")
def approve(body: TrialBody, request: Request, db: Session = Depends(get_db)):
    return mutate(db, request, "approve_nonqualifying_trial", lambda: ledger.approve_trial(
        db, actor=request.state.actor, **body.model_dump(exclude={"acknowledge_nonqualifying_trial"})))


@router.post("/paper/kill-switch/enable")
def stop(request: Request, db: Session = Depends(get_db)):
    return mutate(db, request, "enable_paper_kill_switch", lambda: ledger.set_kill_switch(db, True))


@router.post("/paper/kill-switch/disable")
def resume(request: Request, db: Session = Depends(get_db)):
    return mutate(db, request, "disable_paper_kill_switch", lambda: ledger.set_kill_switch(db, False))


@router.post("/paper/intents")
def reserve(body: IntentBody, request: Request, db: Session = Depends(get_db)):
    return mutate(db, request, "reserve_paper_intent", lambda: ledger.reserve_intent(db, **body.model_dump()))


@router.get("/paper/account")
def account(db: Session = Depends(get_db)):
    row = db.get(PaperExecutionAccount, 1)
    return project(row, ("starting_cash", "cash", "reserved_cash", "quantity", "reserved_quantity", "kill_switch")) if row else None


@router.post("/paper/intents/{intent_id}/abandon")
def abandon(intent_id: int, request: Request, db: Session = Depends(get_db)):
    return mutate(db, request, "abandon_unsubmitted_paper", lambda: ledger.abandon_unsubmitted(db, intent_id))


@router.get("/paper/intents")
def intents(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    return [project(row, ("id", "client_order_id", "decision_id", "approval_id", "side", "quantity", "limit_price",
                         "filled_quantity", "status", "provider_order_id", "created_at"))
            for row in db.scalars(select(PaperOrderIntent).order_by(PaperOrderIntent.id.desc()).limit(limit))]


def provider_operation(db, request, intent_id, *, trade_id=None, submit=False):
    if not settings.freqtrade_paper_execution_enabled or settings.allow_live_trading:
        raise HTTPException(409, "Freqtrade paper execution is disabled")
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    try:
        with FreqtradeDryRunClient(settings.freqtrade_url, settings.freqtrade_username,
                                  settings.freqtrade_password.get_secret_value()) as client:
            result = dispatch_intent(factory, client, intent_id) if submit else reconcile_intent(
                factory, client, intent_id, trade_id=trade_id)
        with factory.begin() as audit_db:
            audit(audit_db, request, "dispatch_paper" if submit else "reconcile_paper", intent_id)
        return result
    except (FreqtradeError, ledger.PaperExecutionError):
        # Do not disclose upstream payloads/configuration or connection details.
        raise HTTPException(409, "Paper operation blocked; inspect persisted intent and reconcile ambiguous submissions, never resend them") from None


@router.post("/paper/intents/{intent_id}/dispatch")
def dispatch(intent_id: int, request: Request, db: Session = Depends(get_db)):
    return provider_operation(db, request, intent_id, submit=True)


@router.post("/paper/intents/{intent_id}/reconcile")
def reconcile_provider(intent_id: int, body: ReconcileBody, request: Request, db: Session = Depends(get_db)):
    return provider_operation(db, request, intent_id, trade_id=body.trade_id)


@router.get("/paper/external-fills")
def external_fills(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    return [project(row, ("id", "entry_intent_id", "provider_order_id", "snapshot_sha256", "quantity", "cost", "fee", "reason", "observed_at"))
            for row in db.scalars(select(PaperExternalFill).order_by(PaperExternalFill.id.desc()).limit(limit))]


@router.get("/paper/performance")
def performance(approval_id: int = Query(..., gt=0), db: Session = Depends(get_db)):
    return paper_performance(db, approval_id)


@router.post("/paper/recover")
def recover(request: Request, db: Session = Depends(get_db)):
    if not settings.freqtrade_paper_execution_enabled or settings.allow_live_trading:
        raise HTTPException(409, "Explicit paper-only configuration required")
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    try:
        with FreqtradeDryRunClient(settings.freqtrade_url, settings.freqtrade_username,
                                  settings.freqtrade_password.get_secret_value()) as client:
            return run_recovery_cycle(factory, client)
    except (FreqtradeError, ledger.PaperExecutionError):
        raise HTTPException(409, "Recovery blocked; inspect persisted recovery audits") from None
