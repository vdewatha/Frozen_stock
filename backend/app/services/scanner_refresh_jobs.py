from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import ScannerRefreshJob
from app.services.audit import write_audit_log
from app.services.notifications import create_notification
from app.services.trade_candidates import get_trade_candidate_snapshot


def _serialize_job(job: Optional[ScannerRefreshJob]) -> Optional[dict]:
    if not job:
        return None
    return {
        "id": job.id,
        "trigger": job.trigger,
        "status": job.status,
        "message": job.message,
        "payload": job.payload or {},
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    }


def latest_scanner_refresh_job(db: Session) -> Optional[dict]:
    job = db.query(ScannerRefreshJob).order_by(ScannerRefreshJob.created_at.desc()).first()
    return jsonable_encoder(_serialize_job(job))


def create_scanner_refresh_job(db: Session, trigger: str = "manual", limit: int = 50, context: Optional[dict] = None) -> dict:
    job = ScannerRefreshJob(
        trigger=trigger,
        status="queued",
        message="Trade candidate scanner refresh queued.",
        payload={"limit": max(1, min(limit, 50)), "context": context or {}},
    )
    db.add(job)
    db.flush()
    write_audit_log(
        db,
        event_type="scanner_refresh_job",
        entity_type="scanner_refresh_job",
        entity_id=job.id,
        action="queue",
        status="queued",
        message=f"Queued trade candidate scanner refresh from {trigger}.",
        payload=job.payload,
    )
    db.commit()
    db.refresh(job)
    return jsonable_encoder(_serialize_job(job))


def run_scanner_refresh_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.query(ScannerRefreshJob).filter(ScannerRefreshJob.id == job_id).one_or_none()
        if not job:
            return
        job.status = "running"
        job.started_at = datetime.utcnow()
        job.message = "Trade candidate scanner refresh running."
        db.commit()

        limit = int((job.payload or {}).get("limit") or 50)
        snapshot = get_trade_candidate_snapshot(db, limit=limit, refresh=True)
        job = db.query(ScannerRefreshJob).filter(ScannerRefreshJob.id == job_id).one()
        job.status = "complete"
        job.completed_at = datetime.utcnow()
        job.message = f"Scanner refreshed {snapshot['candidate_count']} strategy checks with {snapshot['positive_count']} positives."
        job.payload = {
            **(job.payload or {}),
            "candidate_count": snapshot["candidate_count"],
            "positive_count": snapshot["positive_count"],
            "cache_status": snapshot["cache_status"],
            "generated_at": jsonable_encoder(snapshot["generated_at"]),
            "top_candidates": [
                {
                    "symbol": candidate["symbol"],
                    "strategy": candidate["strategy"],
                    "strategy_name": candidate["strategy_name"],
                    "candidate_status": candidate["candidate_status"],
                    "score": candidate["score"],
                }
                for candidate in (snapshot.get("candidates") or [])[:5]
            ],
        }
        write_audit_log(
            db,
            event_type="scanner_refresh_job",
            entity_type="scanner_refresh_job",
            entity_id=job.id,
            action="complete",
            status="complete",
            message=job.message,
            payload=job.payload,
        )
        db.commit()
    except Exception as exc:
        job = db.query(ScannerRefreshJob).filter(ScannerRefreshJob.id == job_id).one_or_none()
        if job:
            job.status = "failed"
            job.completed_at = datetime.utcnow()
            job.message = f"Scanner refresh failed: {exc.__class__.__name__}: {str(exc)[:240]}"
            job.payload = {**(job.payload or {}), "error": job.message}
            create_notification(
                db,
                category="scanner_refresh",
                severity="critical",
                source="scanner_refresh_job",
                title="Scanner refresh failed",
                message=job.message,
                entity_type="scanner_refresh_job",
                entity_id=job.id,
                payload=job.payload,
            )
            db.commit()
    finally:
        db.close()
