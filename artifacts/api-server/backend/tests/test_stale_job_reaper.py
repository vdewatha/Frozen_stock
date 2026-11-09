"""Jobs orphaned in queued/running by a restart are closed, not left forever."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.db.base import Base
from app.models import ScannerRefreshJob
from app.services.scanner_refresh_jobs import expire_stale_scanner_refresh_jobs
from app.tasks.celery_app import celery_app

NOW = datetime(2026, 11, 9, 17, 0)


def test_orphaned_scanner_refresh_jobs_are_failed_with_a_reason():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        stuck = ScannerRefreshJob(status="running", created_at=datetime(2026, 10, 8, 19, 58), started_at=datetime(2026, 10, 8, 19, 58))
        never_started = ScannerRefreshJob(status="queued", created_at=NOW - timedelta(hours=1))
        active = ScannerRefreshJob(status="running", created_at=NOW - timedelta(minutes=20), started_at=NOW - timedelta(minutes=20))
        db.add_all([stuck, never_started, active])
        db.commit()

        result = expire_stale_scanner_refresh_jobs(db, now=NOW)

        assert sorted(result["expired_job_ids"]) == sorted([stuck.id, never_started.id])
        db.refresh(stuck), db.refresh(never_started), db.refresh(active)
        assert stuck.status == "failed" and "process running it stopped" in stuck.message
        assert never_started.status == "failed" and "never started" in never_started.message
        assert active.status == "running"
    engine.dispose()


def test_the_reaper_is_scheduled_under_a_name_hosts_do_not_exclude():
    # .replit excludes scalp-research-queue-recovery; the reaper must not ride on it.
    entry = celery_app.conf.beat_schedule["stale-job-reaper"]
    assert entry["task"] == "app.tasks.jobs.reap_stale_jobs_job"
    assert "stale-job-reaper" not in "scalp-research-batch,scalp-research-queue-recovery,strategy-learning-batch-job,delayed-sip-collection"
