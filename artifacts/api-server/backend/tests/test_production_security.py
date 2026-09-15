import base64
import hashlib
import hmac
import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import AuthenticationMiddleware


def _token(subject: str, secret: str, *, role_claim: str = "viewer", issued_at: int | None = None) -> str:
    issued_at = issued_at or int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {"sub": subject, "role": role_claim, "iat": issued_at, "exp": issued_at + 300}

    def encode(value: dict) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).rstrip(b"=").decode()

    encoded_header, encoded_claims = encode(header), encode(claims)
    signing_input = f"{encoded_header}.{encoded_claims}".encode()
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{encoded_header}.{encoded_claims}.{signature}"


def _client(**overrides) -> TestClient:
    config = {
        "environment": "production",
        "auth_mode": "production_identity",
        "auth_identity_signing_secret": "primary-signing-secret-" * 2,
        "auth_identity_roles": {
            "alice": "viewer",
            "bob": "operator",
            "root": "admin",
        },
        "paper_alpaca_api_key": "paper-key",
        "paper_alpaca_api_secret": "paper-secret",
    }
    config.update(overrides)
    app = FastAPI()
    app.add_middleware(AuthenticationMiddleware, configuration=Settings(_env_file=None, **config))
    app.add_api_route("/dashboard", lambda: {"ok": True}, methods=["GET"])
    app.add_api_route("/safety/kill-switch/enable", lambda: {"ok": True}, methods=["POST"])
    return TestClient(app)


def _headers(subject: str, *, secret: str = "primary-signing-secret-" * 2, key: str = "one") -> dict:
    return {
        "Authorization": f"Bearer {_token(subject, secret)}",
        "X-Action-Confirmation": "confirm",
        "X-Action-Reason": "Reviewed safety state",
        "X-Idempotency-Key": key,
    }


def test_production_identity_maps_subject_server_side_and_rejects_role_escalation():
    client = _client()
    viewer = _headers("alice", key="viewer-read")
    assert client.get("/dashboard", headers=viewer).status_code == 200
    assert client.post(
        "/safety/kill-switch/enable",
        headers={**viewer, "X-Idempotency-Key": "viewer-action"},
    ).status_code == 403
    # A forged role claim does not change the mapped viewer permission.
    forged = _headers("alice", key="forged")
    forged["Authorization"] = f"Bearer {_token('alice', 'primary-signing-secret-' * 2, role_claim='admin')}"
    assert client.post("/safety/kill-switch/enable", headers=forged).status_code == 403


def test_production_mutation_requires_confirmation_and_rejects_replay():
    client = _client()
    operator = _headers("bob", key="same-request")
    missing_confirmation = dict(operator)
    del missing_confirmation["X-Action-Confirmation"]
    assert client.post("/safety/kill-switch/enable", headers=missing_confirmation).status_code == 428
    assert client.post("/safety/kill-switch/enable", headers=operator).status_code == 200
    assert client.post("/safety/kill-switch/enable", headers=operator).status_code == 409


def test_production_configuration_fails_closed_and_previous_secret_supports_rotation():
    missing = _client(
        auth_identity_signing_secret="",
        paper_alpaca_api_key="",
        paper_alpaca_api_secret="",
    )
    assert missing.get("/dashboard", headers=_headers("alice")).status_code == 503

    previous = "previous-signing-secret-" * 2
    client = _client(
        auth_identity_signing_secret="new-signing-secret-" * 2,
        auth_identity_previous_signing_secret=previous,
    )
    assert client.get("/dashboard", headers=_headers("alice", secret=previous, key="rotated")).status_code == 200