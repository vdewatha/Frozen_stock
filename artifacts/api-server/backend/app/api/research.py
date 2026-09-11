"""Read-only registry summaries. Artifact paths and executable files stay local."""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import ResearchModelRun

router = APIRouter(prefix="/research", tags=["research"])


class ModelMetrics(BaseModel):
    brier_score: float
    log_loss: float


class RunSummary(BaseModel):
    run_id: str
    status: Literal["experimental"]
    eligible_for_trading: Literal[False]
    created_at: datetime
    symbol: str
    horizon_bars: int
    train_rows: int
    holdout_rows: int
    train_end: datetime
    train_label_end: datetime
    holdout_start: datetime
    holdout_end: datetime
    metrics: dict[str, ModelMetrics]
    versions: dict[str, str]


class RunPage(BaseModel):
    items: list[RunSummary]
    total: int
    limit: int
    offset: int


def summarize(record: ResearchModelRun) -> dict:
    metadata = record.training_metadata
    # Explicit projection: do not serialize the ORM record or arbitrary metadata.
    return {
        "run_id": record.run_id, "status": record.status,
        "eligible_for_trading": record.eligible_for_trading,
        "created_at": record.created_at,
        **{key: metadata[key] for key in ("symbol", "horizon_bars", "train_rows", "holdout_rows", "train_end", "train_label_end", "holdout_start", "holdout_end")},
        "metrics": {name: {key: metadata["metrics"][name][key] for key in ("brier_score", "log_loss")}
                    for name in ("logistic_regression", "random_forest", "training_prevalence_baseline")},
        "versions": {key: metadata["versions"][key] for key in ("python", "numpy", "pandas", "sklearn", "joblib", "platform", "machine") if key in metadata["versions"]},
    }


@router.get("/runs", response_model=RunPage)
def list_research_runs(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    records = db.scalars(select(ResearchModelRun).order_by(ResearchModelRun.id.desc()).offset(offset).limit(limit)).all()
    return {"items": [summarize(record) for record in records],
            "total": db.scalar(select(func.count()).select_from(ResearchModelRun)), "limit": limit, "offset": offset}


@router.get("/runs/{run_id}", response_model=RunSummary)
def research_run(run_id: str = Path(..., pattern=r"^[0-9a-f]{64}$"), db: Session = Depends(get_db)):
    record = db.scalar(select(ResearchModelRun).where(ResearchModelRun.run_id == run_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Research run not found")
    return summarize(record)
