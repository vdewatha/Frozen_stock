from datetime import date
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import StockLearningCycleEvent
from app.services.stock_learning_cycle import (
    create_learning_cycle,
    cycle_projection,
    review_learning_cycle,
)


def _db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def test_scheduled_cycle_is_visible_as_deferred_and_does_not_claim_coverage():
    db = _db()
    with patch(
        "app.services.stock_learning_cycle.evaluate_cycle_prerequisites",
        return_value={
            "verified_feed": {"status": "unknown", "reason": "regular session required"},
            "scheduler_health": {"status": "pass"},
            "paper_ledger": {"status": "pass"},
            "dataset_provenance": {"status": "pass"},
            "readiness_history": {"status": "pass"},
        },
    ):
        cycle, duplicate = create_learning_cycle(
            db,
            symbols=["SPY"],
            cutoff_at=date(2026, 9, 12),
            horizon_days=5,
            provider="yfinance",
            actor="scheduler",
            trigger="scheduled",
        )
        db.commit()

    assert duplicate is False
    assert cycle.status == "deferred"
    assert cycle.training_job_id is None
    assert cycle.gates["verified_feed"]["status"] == "unknown"
    assert len(db.scalars(select(StockLearningCycleEvent)).all()) == 1

    same, duplicate = create_learning_cycle(
        db,
        symbols=["SPY"],
        cutoff_at=date(2026, 9, 12),
        horizon_days=5,
        provider="yfinance",
        actor="scheduler",
        trigger="scheduled",
    )
    assert duplicate is True
    assert same.cycle_id == cycle.cycle_id
    db.close()


def test_cycle_review_remains_blocked_without_registered_validation_and_forward_evidence():
    db = _db()
    with patch(
        "app.services.stock_learning_cycle.evaluate_cycle_prerequisites",
        return_value={
            "verified_feed": {"status": "fail", "reason": "feed unavailable"},
            "scheduler_health": {"status": "pass"},
            "paper_ledger": {"status": "pass"},
            "dataset_provenance": {"status": "pass"},
            "readiness_history": {"status": "pass"},
        },
    ):
        cycle, _ = create_learning_cycle(
            db,
            symbols=["SPY"],
            cutoff_at=date(2026, 9, 12),
            horizon_days=5,
            provider="yfinance",
            actor="operator",
        )
    reviewed = review_learning_cycle(
        db, cycle.cycle_id, actor="operator", reason="Evidence review requested"
    )
    db.commit()
    projection = cycle_projection(db, reviewed)
    assert reviewed.status == "blocked"
    assert projection["paper_only"] is True
    assert projection["live_authorized"] is False
    assert projection["gates"]["validation"]["status"] == "fail"
    assert projection["gates"]["forward_trial"]["status"] == "unknown"
    assert len(projection["events"]) == 2
    db.close()