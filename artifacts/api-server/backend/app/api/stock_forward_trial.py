"""Admin/operator controls for controlled stock forward-paper trials."""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models import StockPaperTrial, StockPaperModelBinding, StockModelRegistry, StockDatasetSnapshot, StockPaperTrialDecision, StockPaperTrialMetric
from app.services.stock_forward_trial import StockTrainingError, create_trial, start_trial, pause_trial, stop_trial

router = APIRouter(prefix="/stock/forward-trials", tags=["stock forward trials"])

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
class Approve(Strict):
    binding_id: int = Field(gt=0)
class Reason(Strict):
    reason: str = Field(min_length=3, max_length=500)
def _out(x):
    return {"id": x.id, "status": x.status, "binding_id": x.binding_id, "policy": x.policy,
            "lineage": x.lineage, "blocked_reason": x.blocked_reason, "pause_reason": x.pause_reason,
            "started_at": x.started_at, "stopped_at": x.stopped_at,
            "baseline_equity": x.baseline_equity, "baseline_at": x.baseline_at,
            "peak_equity": x.peak_equity}

def _call(fn, *args, **kwargs):
    try: return fn(*args, **kwargs)
    except StockTrainingError as exc: raise HTTPException(409, str(exc)) from None

@router.get("")
def list_trials(db: Session = Depends(get_db)):
    return {"items": [_out(x) for x in db.scalars(select(StockPaperTrial).order_by(StockPaperTrial.created_at.desc())).all()]}

@router.get("/bindings/eligible")
def eligible_bindings(db: Session = Depends(get_db)):
    rows = db.scalars(select(StockPaperModelBinding).order_by(StockPaperModelBinding.id.desc())).all()
    result = []
    for row in rows:
        model, snapshot = db.get(StockModelRegistry, row.model_run_id), db.get(StockDatasetSnapshot, row.snapshot_id)
        eligible = bool(model and snapshot and row.paper_only and not row.live_authorized and
                        snapshot.metadata_json.get("binding_eligible", False))
        result.append({"binding_id": row.id, "model_run_id": row.model_run_id, "snapshot_id": row.snapshot_id,
                       "eligible": eligible, "reason": None if eligible else "binding, snapshot, or eligibility evidence unavailable",
                       "paper_only": True, "live_authorized": False})
    return {"items": result}

@router.get("/{trial_id}")
def get_trial(trial_id: str, db: Session = Depends(get_db)):
    row = db.get(StockPaperTrial, trial_id)
    if not row: raise HTTPException(404, "Trial not found")
    return _out(row)

@router.get("/{trial_id}/decisions")
def decisions(trial_id: str, db: Session = Depends(get_db)):
    if not db.get(StockPaperTrial, trial_id): raise HTTPException(404, "Trial not found")
    rows = db.scalars(select(StockPaperTrialDecision).where(StockPaperTrialDecision.trial_id == trial_id).order_by(StockPaperTrialDecision.bar_timestamp)).all()
    return {"items": [{"id": x.id, "symbol": x.symbol, "bar_timestamp": x.bar_timestamp, "action": x.action,
                       "qualifying": x.qualifying, "rejection_reason": x.rejection_reason, "lineage": x.lineage,
                       "order_id": x.order_id} for x in rows]}

@router.get("/{trial_id}/metrics")
def metrics(trial_id: str, db: Session = Depends(get_db)):
    if not db.get(StockPaperTrial, trial_id): raise HTTPException(404, "Trial not found")
    return {"items": [{"as_of": x.as_of, "classification": x.classification, "payload": x.payload}
                      for x in db.scalars(select(StockPaperTrialMetric).where(StockPaperTrialMetric.trial_id == trial_id).order_by(StockPaperTrialMetric.as_of)).all()]}

@router.post("")
def approve(body: Approve, request: Request, db: Session = Depends(get_db)):
    row = _call(create_trial, db, binding_id=body.binding_id, actor=request.state.actor)
    db.commit(); return _out(row)

@router.post("/{trial_id}/start")
def start(trial_id: str, db: Session = Depends(get_db)):
    row = _call(start_trial, db, trial_id); db.commit(); return _out(row)

@router.post("/{trial_id}/pause")
def pause(trial_id: str, body: Reason, db: Session = Depends(get_db)):
    row = _call(pause_trial, db, trial_id, body.reason); db.commit(); return _out(row)

@router.post("/{trial_id}/resume")
def resume(trial_id: str, db: Session = Depends(get_db)):
    row = _call(start_trial, db, trial_id); db.commit(); return _out(row)

@router.post("/{trial_id}/stop")
def stop(trial_id: str, db: Session = Depends(get_db)):
    row = _call(stop_trial, db, trial_id); db.commit(); return _out(row)
