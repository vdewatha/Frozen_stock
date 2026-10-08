"""Route-level contract tests for the authenticated agent research runs API."""
from __future__ import annotations

from datetime import date
import json
import os
from unittest.mock import patch
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.agent_research import router
from app.core.config import Settings
from app.core.security import AuthenticationMiddleware
from app.db.base import Base
from app.db.session import get_db
from app.models import AgentResearchRun, MarketPrice, NewsArticle
from app.services.agent_research import run_agent_research


PATH = "/api/research/agent-runs"
VIEWER_KEY = "v" * 32
RESEARCHER_KEY = "r" * 32
OPERATOR_KEY = "o" * 32
ADMIN_KEY = "a" * 32


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _limit):
        return json.dumps(self.payload).encode()


@pytest.fixture
def research_api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            AgentResearchRun.__table__,
            MarketPrice.__table__,
            NewsArticle.__table__,
        ],
    )
    with Session(engine) as db:
        db.add(
            MarketPrice(
                symbol="AAPL",
                price_date=date(2026, 9, 16),
                close="250.12",
                volume=1000,
                source="synthetic-unit-fixture",
            )
        )
        db.commit()

    app = FastAPI()
    app.add_middleware(
        AuthenticationMiddleware,
        configuration=Settings(
            _env_file=None,
            auth_viewer_key=VIEWER_KEY,
            auth_researcher_key=RESEARCHER_KEY,
            auth_operator_key=OPERATOR_KEY,
            auth_admin_key=ADMIN_KEY,
        ),
    )
    app.include_router(router, prefix="/api")

    def session():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = session
    with (
        TestClient(app) as client,
        patch("app.api.agent_research.write_audit_log") as audit,
        patch("app.api.agent_research.agent_research_job.apply_async") as dispatch,
    ):
        yield client, engine, audit, dispatch
    engine.dispose()


def headers(role: str = "viewer") -> dict[str, str]:
    keys = {
        "viewer": VIEWER_KEY,
        "researcher": RESEARCHER_KEY,
        "operator": OPERATOR_KEY,
        "admin": ADMIN_KEY,
    }
    return {"Authorization": f"Bearer {keys[role]}"}


def run_count(engine) -> int:
    with Session(engine) as db:
        return db.scalar(select(func.count()).select_from(AgentResearchRun))


def test_viewer_cannot_start_run_and_does_not_create_or_dispatch(research_api):
    client, engine, audit, dispatch = research_api

    response = client.post(PATH, json={"symbol": "AAPL"}, headers=headers())

    assert response.status_code == 403
    assert run_count(engine) == 0
    audit.assert_not_called()
    dispatch.assert_not_called()


def test_researcher_start_is_queued_and_duplicate_dispatches_once(research_api):
    client, engine, audit, dispatch = research_api

    first_response = client.post(PATH, json={"symbol": "aapl"}, headers=headers("researcher"))
    assert first_response.status_code == 200, first_response.text
    first = first_response.json()
    assert UUID(first["run_id"])
    assert first["symbol"] == "AAPL"
    assert first["status"] == "queued"
    assert first["result"] is None
    assert first["eligible_for_trading"] is False
    assert first["deduplicated"] is False
    dispatch.assert_called_once_with(args=[first["run_id"]])
    audit.assert_called_once()

    duplicate_response = client.post(
        PATH,
        json={"symbol": "AAPL"},
        headers=headers("researcher"),
    )
    assert duplicate_response.status_code == 200, duplicate_response.text
    duplicate = duplicate_response.json()
    assert duplicate["run_id"] == first["run_id"]
    assert duplicate["status"] == "queued"
    assert duplicate["deduplicated"] is True
    assert run_count(engine) == 1
    dispatch.assert_called_once_with(args=[first["run_id"]])
    assert audit.call_count == 2


def test_unsupported_symbol_and_extra_input_are_rejected_without_side_effects(research_api):
    client, engine, audit, dispatch = research_api

    unsupported = client.post(
        PATH,
        json={"symbol": "TSLA"},
        headers=headers("researcher"),
    )
    assert unsupported.status_code == 409
    assert "approved four-symbol" in unsupported.json()["detail"]
    assert run_count(engine) == 0
    audit.assert_not_called()
    dispatch.assert_not_called()

    extra_input = client.post(
        PATH,
        json={"symbol": "AAPL", "eligible_for_trading": True},
        headers=headers("researcher"),
    )
    assert extra_input.status_code == 422
    assert run_count(engine) == 0
    audit.assert_not_called()
    dispatch.assert_not_called()


def test_missing_provider_is_persisted_unavailable_and_readable_by_viewer(research_api):
    client, engine, _, dispatch = research_api
    created = client.post(PATH, json={"symbol": "AAPL"}, headers=headers("researcher")).json()

    with patch.dict(os.environ, {}, clear=True):
        with Session(engine) as db:
            result = run_agent_research(db, created["run_id"])

    assert result["status"] == "unavailable"
    dispatch.assert_called_once()
    fetched = client.get(f"{PATH}/{created['run_id']}", headers=headers())
    assert fetched.status_code == 200
    run = fetched.json()
    assert run["status"] == "unavailable"
    assert run["result"] is None
    assert "provider is unavailable" in run["error"]


def test_malformed_provider_response_is_persisted_failed_and_readable_by_viewer(research_api):
    client, engine, _, dispatch = research_api
    created = client.post(PATH, json={"symbol": "AAPL"}, headers=headers("researcher")).json()
    malformed = {"choices": [{"message": {"content": '{"recommendation":"BUY"}'}}]}

    with (
        patch.dict(
            os.environ,
            {
                "AI_INTEGRATIONS_OPENAI_BASE_URL": "https://provider.test/v1",
                "AI_INTEGRATIONS_OPENAI_API_KEY": "synthetic-unit-key",
            },
            clear=True,
        ),
        patch(
            "app.services.agent_research.urlopen",
            return_value=FakeResponse(malformed),
        ),
    ):
        with Session(engine) as db:
            result = run_agent_research(db, created["run_id"])

    assert result["status"] == "failed"
    dispatch.assert_called_once()
    fetched = client.get(f"{PATH}/{created['run_id']}", headers=headers())
    assert fetched.status_code == 200
    run = fetched.json()
    assert run["status"] == "failed"
    assert run["result"] is None
    assert "malformed structured research output" in run["error"]


def test_valid_provider_response_is_persisted_completed_and_readable_by_viewer(research_api):
    client, engine, _, dispatch = research_api
    created = client.post(PATH, json={"symbol": "AAPL"}, headers=headers("researcher")).json()
    payload = {
        "model": "gpt-5.6-terra",
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "recommendation": "HOLD",
                            "rationale": "Bounded synthetic research response.",
                            "confidence": 0.55,
                            "limitations": ["Research only"],
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
    }

    with (
        patch.dict(
            os.environ,
            {
                "AI_INTEGRATIONS_OPENAI_BASE_URL": "https://provider.test/v1",
                "AI_INTEGRATIONS_OPENAI_API_KEY": "synthetic-unit-key",
            },
            clear=True,
        ),
        patch(
            "app.services.agent_research.urlopen",
            return_value=FakeResponse(payload),
        ),
    ):
        with Session(engine) as db:
            result = run_agent_research(db, created["run_id"])

    assert result["status"] == "completed"
    dispatch.assert_called_once()
    fetched = client.get(f"{PATH}/{created['run_id']}", headers=headers())
    assert fetched.status_code == 200
    run = fetched.json()
    assert run["status"] == "completed"
    assert run["result"]["recommendation"] == "HOLD"
    assert run["result"]["confidence"] == 0.55
    assert run["eligible_for_trading"] is False
