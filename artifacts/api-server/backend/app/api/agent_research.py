"""Authenticated control-room access to bounded, non-trading agent research."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import AgentResearchRun
from app.services.agent_research import (
    AgentResearchError,
    create_agent_research_run,
    project_agent_research,
    refresh_agent_research_evaluation,
)
from app.services.audit import write_audit_log
from app.tasks.jobs import agent_research_job

router = APIRouter(prefix="/research", tags=["agent research"])


class StartAgentResearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=16)


@router.post("/agent-runs")
def start_agent_research(
    body: StartAgentResearchBody, request: Request, db: Session = Depends(get_db)
) -> dict:
    try:
        row, duplicate = create_agent_research_run(db, symbol=body.symbol, actor=request.state.actor)
        write_audit_log(
            db,
            event_type="agent_research",
            action="start_deduplicated" if duplicate else "start",
            status="success",
            message="Bounded research-only TradingAgents shadow run requested",
            entity_type="agent_research_run",
            entity_id=row.id,
            payload={"actor": request.state.actor, "request_id": request.state.request_id, "symbol": row.symbol},
        )
        db.commit()
        if not duplicate:
            agent_research_job.apply_async(args=[row.run_id], queue="learning")
        db.refresh(row)
        return project_agent_research(row) | {"deduplicated": duplicate}
    except AgentResearchError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/agent-runs")
def list_agent_research(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    rows = db.scalars(
        select(AgentResearchRun)
        .order_by(AgentResearchRun.created_at.desc(), AgentResearchRun.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    changed = sum(refresh_agent_research_evaluation(db, row) for row in rows)
    if changed:
        db.commit()
    return {
        "items": [project_agent_research(row) for row in rows],
        "total": db.scalar(select(func.count()).select_from(AgentResearchRun)),
        "limit": limit,
        "offset": offset,
    }


@router.get("/agent-runs/{run_id}")
def get_agent_research(
    run_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$"),
    db: Session = Depends(get_db),
) -> dict:
    row = db.scalar(select(AgentResearchRun).where(AgentResearchRun.run_id == run_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Agent research run not found")
    if refresh_agent_research_evaluation(db, row):
        db.commit()
    return project_agent_research(row)