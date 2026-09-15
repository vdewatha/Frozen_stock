import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import (
    AuthenticationMiddleware,
    secondary_approval_identity,
)
from tests.test_production_security import _token


def _settings() -> Settings:
    roles = {
        "primary": "admin", "secondary": "admin", "operator": "operator",
        "viewer": "viewer", "service": "researcher", "worker": "researcher",
        "scheduler": "researcher", "emergency": "operator",
    }
    return Settings(
        _env_file=None,
        environment="production",
        auth_mode="production_identity",
        auth_identity_signing_secret="secondary-test-secret-" * 2,
        auth_identity_roles=roles,
        auth_identity_principal_types={
            "primary": "reviewer", "secondary": "reviewer", "operator": "operator",
            "viewer": "viewer", "service": "service", "worker": "worker",
            "scheduler": "scheduler", "emergency": "emergency",
        },
        auth_identity_issuer="identity.test",
        auth_identity_audience="trading-api",
        paper_alpaca_api_key="paper-key",
        paper_alpaca_api_secret="paper-secret",
        paper_broker_account_id="paper-account",
    )


def _client() -> TestClient:
    configuration = _settings()
    app = FastAPI()
    app.add_middleware(AuthenticationMiddleware, configuration=configuration)

    @app.post("/sensitive")
    def sensitive(request: Request):
        try:
            return secondary_approval_identity(
                request, minimum_role="admin", configuration=configuration
            )
        except ValueError as exc:
            raise HTTPException(403, str(exc)) from None

    return TestClient(app)


def _bearer(subject: str) -> str:
    return _token(subject, "secondary-test-secret-" * 2, issued_at=int(time.time()))


def test_distinct_server_mapped_secondary_admin_is_accepted_and_redacted():
    response = _client().post(
        "/sensitive",
        headers={
            "Authorization": f"Bearer {_bearer('primary')}",
            "X-Secondary-Authorization": f"Bearer {_bearer('secondary')}",
            "X-Action-Confirmation": "confirm",
            "X-Action-Reason": "Two-person approval test",
            "X-Idempotency-Key": "dual-approval",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "actor": "secondary",
        "role": "admin",
        "method": "identity_assertion",
        "issuer": "identity.test",
    }
    assert "Bearer" not in response.text


def test_same_identity_unmapped_and_underprivileged_secondary_are_rejected():
    for secondary in ("primary", "viewer", "unknown"):
        response = _client().post(
            "/sensitive",
            headers={
                "Authorization": f"Bearer {_bearer('primary')}",
                "X-Secondary-Authorization": f"Bearer {_bearer(secondary)}",
                "X-Action-Confirmation": "confirm",
                "X-Action-Reason": "Negative two-person approval test",
                "X-Idempotency-Key": f"dual-{secondary}",
            },
        )
        assert response.status_code == 403