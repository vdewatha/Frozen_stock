from datetime import date
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.api.stock_learning_cycle as learning_cycle_api
from app.core.config import Settings
from app.core.security import AuthenticationMiddleware, required_role
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


def _client() -> TestClient:
    keys = {
        f"auth_{role}_key": role * 16
        for role in ("viewer", "researcher", "operator", "admin")
    }
    app = FastAPI()
    app.add_middleware(
        AuthenticationMiddleware,
        configuration=Settings(_env_file=None, **keys),
    )
    app.include_router(learning_cycle_api.router)
    app.dependency_overrides[learning_cycle_api.get_db] = lambda: MagicMock()
    return TestClient(app)


def _headers(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {role * 16}"}


def test_learning_cycle_route_roles_cover_list_review_and_action():
    cycle_id = "a" * 64
    assert required_role("GET", "/stock/learning-cycles") == "viewer"
    assert required_role("GET", f"/stock/learning-cycles/{cycle_id}") == "viewer"
    assert required_role("POST", "/stock/learning-cycles") == "researcher"
    assert required_role("POST", f"/stock/learning-cycles/{cycle_id}/review") == "operator"
    assert required_role("POST", f"/stock/learning-cycles/{cycle_id}/action") == "operator"

    with (
        patch.object(learning_cycle_api, "list_learning_cycles", return_value=[]),
        patch.object(learning_cycle_api, "review_learning_cycle", return_value=object()),
        patch.object(learning_cycle_api, "cycle_action", return_value=object()),
        patch.object(learning_cycle_api, "cycle_projection", return_value={"ok": True}),
        patch.object(learning_cycle_api, "_audit"),
    ):
        with _client() as client:
            for role in ("viewer", "researcher", "operator", "admin"):
                with patch.object(
                    learning_cycle_api,
                    "list_learning_cycles",
                    return_value=[],
                ):
                    response = client.get(
                        "/stock/learning-cycles", headers=_headers(role)
                    )
                assert response.status_code == 200, (role, response.text)

                review = client.post(
                    f"/stock/learning-cycles/{cycle_id}/review",
                    headers=_headers(role),
                    json={"reason": "review evidence"},
                )
                action = client.post(
                    f"/stock/learning-cycles/{cycle_id}/action",
                    headers=_headers(role),
                    json={"action": "promote", "reason": "explicit promotion"},
                )
                expected = 200 if role in {"operator", "admin"} else 403
                assert review.status_code == expected, (role, review.text)
                assert action.status_code == expected, (role, action.text)


def test_learning_cycle_action_payload_cannot_inject_actor_or_automatic_promotion():
    cycle_id = "b" * 64
    with (
        patch.object(learning_cycle_api, "cycle_action", return_value=object()),
        patch.object(learning_cycle_api, "cycle_projection", return_value={"ok": True}),
        patch.object(learning_cycle_api, "_audit"),
    ):
        with _client() as client:
            for payload in (
                {"action": "promote", "reason": "valid reason", "actor": "admin"},
                {"action": "automatic_promote", "reason": "valid reason"},
                {"action": "promote", "reason": "   "},
            ):
                response = client.post(
                    f"/stock/learning-cycles/{cycle_id}/action",
                    headers=_headers("operator"),
                    json=payload,
                )
                assert response.status_code == 422, (payload, response.text)

            review = client.post(
                f"/stock/learning-cycles/{cycle_id}/review",
                headers=_headers("operator"),
                json={"reason": "  "},
            )
            assert review.status_code == 422


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