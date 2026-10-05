"""Authenticated report persistence contract, without any provider or broker calls."""
from datetime import date, datetime, timezone
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.agent_research import router
from app.core.config import Settings
from app.core.security import AuthenticationMiddleware
from app.db.base import Base
from app.db.session import get_db
from app.models import AgentResearchReport, AgentResearchRun, MarketPrice, ModelPrediction


@pytest.fixture
def report_api():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        model.__table__ for model in (AgentResearchReport, AgentResearchRun, MarketPrice, ModelPrediction)
    ])
    app = FastAPI()
    app.add_middleware(AuthenticationMiddleware, configuration=Settings(
        _env_file=None, auth_viewer_key="v" * 32, auth_researcher_key="r" * 32,
        auth_operator_key="o" * 32, auth_admin_key="a" * 32,
    ))
    app.include_router(router, prefix="/api")

    def session():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = session
    with TestClient(app) as client, patch("app.api.agent_research.write_audit_log") as audit:
        yield client, engine, audit
    engine.dispose()


def headers(role="viewer"):
    return {"Authorization": "Bearer " + ("v" if role == "viewer" else "r") * 32}


PATH = "/api/research/agent-comparison-reports"


def test_permissions_input_validation_and_read_only_history(report_api):
    client, engine, audit = report_api
    assert client.get(PATH).status_code == 401
    assert client.post(PATH, json={}, headers=headers()).status_code == 403
    assert client.post(PATH, json={"eligible_for_trading": True}, headers=headers("researcher")).status_code == 422
    assert client.get(PATH + "?limit=21", headers=headers()).status_code == 422
    saved = client.post(PATH, json={}, headers=headers("researcher"))
    assert saved.status_code == 200, saved.text
    snapshot = saved.json()
    assert snapshot["report"]["status"] == "pending"
    assert snapshot["report"]["eligible_for_trading"] is False
    assert snapshot["report"]["metrics"]["agent_directional_accuracy"] is None
    audit.assert_called_once()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        page = client.get(PATH, headers=headers()).json()
        assert page["total"] == 1
        assert page["items"][0] == snapshot
        fetched = client.get(f"{PATH}/{snapshot['report_id']}", headers=headers())
        assert fetched.status_code == 200
        assert fetched.json() == snapshot
        assert client.get(f"{PATH}/{uuid4()}", headers=headers()).status_code == 404
        assert not any(query.startswith(("INSERT", "UPDATE", "DELETE")) for query in statements)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert client.post(PATH, json={}, headers=headers("researcher")).json()["report_id"] == snapshot["report_id"]


def test_saved_report_recomputes_labels_and_exact_baseline_and_preserves_history(report_api):
    client, engine, _ = report_api
    with Session(engine) as db:
        row = AgentResearchRun(
            run_id=str(uuid4()), dedupe_key="1" * 64, symbol="AAPL",
            requested_by="researcher", status="completed",
            framework_version="framework-v1", prompt_version="prompt-v1", model_name="agent-v1",
            source_snapshot=[{"kind": "daily_price", "observed_at": "2020-01-06", "close": "100", "source": "fixture"}],
            result={"recommendation": "BUY", "confidence": 0.8},
            evaluation={"status": "pending"}, usage={},
            decision_at=datetime(2020, 1, 6, 22, tzinfo=timezone.utc),
        )
        db.add(row)
        db.commit()
    first = client.post(PATH, json={}, headers=headers("researcher")).json()
    assert first["report"]["coverage"] == {"matched": 0, "pending": 1, "unavailable": 0, "hold": 0}

    with Session(engine) as db:
        for day in [7, 8, 9, 10, 13]:
            db.add(MarketPrice(symbol="AAPL", price_date=date(2020, 1, day), close="110", source="fixture",
                               imported_at=datetime(2020, 1, day, 22)))
        db.add(ModelPrediction(
            symbol="AAPL", prediction_date=date(2020, 1, 6), horizon_days=5,
            probability_up="0.8", probability_down="0.2", expected_return="0.1",
            source="existing-model-v2", features={"model_version": "v2"},
            created_at=datetime(2020, 1, 6, 21),
        ))
        db.commit()
    second_response = client.post(PATH, json={}, headers=headers("researcher"))
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()
    assert second["report_id"] != first["report_id"]
    assert second["report"]["status"] == "complete"
    assert second["report"]["coverage"]["matched"] == 1
    assert second["report"]["metrics"] == {
        "agent_directional_accuracy": 1.0, "baseline_directional_accuracy": 1.0,
        "baseline_brier_score": 0.04, "directional_pair_count": 1,
    }
    item = second["report"]["observations"][0]
    assert item["source_cutoff"] == "2020-01-06"
    assert item["outcome_date"] == "2020-01-13"
    assert item["baseline"]["model_version"] == "v2"
    assert item["baseline"]["directional_correct"] is True
    assert len(item["outcome_observations"]) == 5
    assert "brier_score" not in item["agent"]
    assert client.get(f"{PATH}/{first['report_id']}", headers=headers()).json() == first
    assert client.post(PATH, json={}, headers=headers("researcher")).json()["report_id"] == second["report_id"]
    assert client.get(PATH + "?limit=1&offset=1", headers=headers()).json()["items"] == [first]
    with Session(engine) as db:
        assert all(not row.eligible_for_trading for row in db.scalars(select(AgentResearchRun)))
