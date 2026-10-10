"""Separate paper-only 1-minute scalp research controls."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import ScalpResearchRun
from app.services.scalp_research import create_scalp_research_run, project_scalp_research
from app.tasks.jobs import scalp_research_job

router = APIRouter(prefix="/research/scalp", tags=["scalp research"])


class StartScalpResearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbols: list[str] = Field(default=["AAPL", "MSFT", "SPY", "QQQ"], min_length=1, max_length=4)


@router.post("/runs", status_code=202)
def start_scalp_research(body: StartScalpResearchBody, request: Request, db: Session = Depends(get_db)):
    try:
        row = create_scalp_research_run(db, symbols=body.symbols, actor=request.state.actor)
        db.commit()
        scalp_research_job.apply_async(args=[row.run_id], queue="scalp_research")
        db.refresh(row)
        return project_scalp_research(row)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/runs")
def list_scalp_research(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    rows = db.scalars(select(ScalpResearchRun).order_by(ScalpResearchRun.created_at.desc()).limit(limit)).all()
    return {"items": [project_scalp_research(row) for row in rows], "paper_only": True}


@router.get("/runs/{run_id}")
def get_scalp_research(run_id: str, db: Session = Depends(get_db)):
    row = db.scalar(select(ScalpResearchRun).where(ScalpResearchRun.run_id == run_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Scalp research run not found")
    return project_scalp_research(row)
