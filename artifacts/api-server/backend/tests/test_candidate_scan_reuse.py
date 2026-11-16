"""The scheduled candidate scan reuses the snapshot the feature run just built."""
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models import TradeCandidateSnapshot
import app.models.stock_paper  # noqa: F401
from app.services import trade_candidates
from app.tasks import jobs

SCANNED = {
    "generated_at": "now", "cache_status": "fresh", "trusted_data_version": 1, "candidate_count": 3,
    "blocked_assets": [], "positive_count": 1, "research_positive_count": 1, "candidates": [],
}


def run_with_snapshot(age: timedelta | None) -> tuple[dict, int]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        if age is not None:
            db.add(TradeCandidateSnapshot(
                payload={**SCANNED, "candidate_count": 7}, source="scanner", created_at=datetime.utcnow() - age,
            ))
            db.commit()
        with patch.object(jobs, "_run_job", side_effect=lambda _name, work: work(db)), \
             patch.object(trade_candidates, "scan_trade_candidates", return_value=dict(SCANNED)) as scan:
            return jobs.trade_candidate_scan_job(), scan.call_count
    finally:
        db.close()
        engine.dispose()


def test_a_snapshot_from_the_last_feature_runs_is_reused():
    # 03:00 ET sees the 17:45 run's snapshot (about 6 h old); 18:00 ET sees the 02:45 run's (about 12.5 h).
    for age in (timedelta(hours=6), timedelta(hours=12, minutes=30)):
        result, scans = run_with_snapshot(age)
        assert scans == 0
        assert result["cache_status"] == "cached"
        assert result["candidate_count"] == 7


def test_it_scans_when_no_recent_snapshot_exists():
    for age in (None, timedelta(hours=17), timedelta(days=2)):
        result, scans = run_with_snapshot(age)
        assert scans == 1
        assert result["cache_status"] == "fresh"
        assert result["candidate_count"] == 3


def test_the_feature_run_still_rebuilds_the_snapshot():
    import inspect

    source = inspect.getsource(jobs.daily_feature_generation)
    assert "get_trade_candidate_snapshot(db, limit=12, refresh=True)" in source
