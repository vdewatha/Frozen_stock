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
from app.services.agent_research_reports import (
    create_agent_research_report,
    get_agent_research_report,
    list_agent_research_reports,
)
from app.tasks.jobs import agent_research_job

router = APIRouter(prefix="/research", tags=["agent research"])


class StartAgentResearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=16)


class CreateComparisonReportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.post("/agent-comparison-reports")
def create_comparison_report(
    body: CreateComparisonReportBody, request: Request, db: Session = Depends(get_db)
) -> dict:
    report = create_agent_research_report(db)
    write_audit_log(
        db,
        event_type="agent_research",
        action="comparison_report",
        status="success",
        message="Research-only matched comparison snapshot saved; no trading authority",
        entity_type="agent_research_report",
        payload={
            "report_id": report["report_id"],
            "content_sha256": report["content_sha256"],
            "actor": request.state.actor,
            "request_id": request.state.request_id,
        },
    )
    db.commit()
    return report


@router.get("/agent-comparison-reports")
def list_comparison_reports(
    limit: int = Query(10, ge=1, le=20),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    return list_agent_research_reports(db, limit=limit, offset=offset)


@router.get("/agent-comparison-reports/{report_id}")
def get_comparison_report(
    report_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$"),
    db: Session = Depends(get_db),
) -> dict:
    report = get_agent_research_report(db, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Agent comparison report not found")
    return report


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
            # Keep bounded agent research on the dedicated research worker so
            # a learning backlog cannot leave user-requested research queued.
            agent_research_job.apply_async(args=[row.run_id])
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
