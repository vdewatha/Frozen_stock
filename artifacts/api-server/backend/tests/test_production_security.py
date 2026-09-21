import base64
import hashlib
import hmac
import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import AuthenticationMiddleware


def _token(
    subject: str,
    secret: str,
    *,
    role_claim: str = "viewer",
    issued_at: int | None = None,
    expires_at: int | None = None,
    issuer: str = "identity.test",
    audience: str = "trading-api",
) -> str:
    issued_at = issued_at or int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "sub": subject, "role": role_claim, "iat": issued_at,
        "exp": expires_at or issued_at + 300, "iss": issuer, "aud": audience,
    }

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
            "reviewer": "admin",
            "service": "researcher",
            "worker": "researcher",
            "scheduler": "researcher",
            "emergency": "operator",
        },
        "auth_identity_principal_types": {
            "alice": "viewer",
            "bob": "operator",
            "root": "reviewer",
            "reviewer": "reviewer",
            "service": "service",
            "worker": "worker",
            "scheduler": "scheduler",
            "emergency": "emergency",
        },
        "auth_identity_issuer": "identity.test",
        "auth_identity_audience": "trading-api",
        "paper_alpaca_api_key": "paper-key",
        "paper_alpaca_api_secret": "paper-secret",
        "paper_broker_account_id": "paper-account",
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


def test_expiration_revocation_issuer_audience_and_lifetime_fail_closed():
    now = int(time.time())
    assert _client().get(
        "/dashboard",
        headers={"Authorization": f"Bearer {_token('alice', 'primary-signing-secret-' * 2, issued_at=now - 400, expires_at=now - 1)}"},
    ).status_code == 401
    assert _client(auth_identity_revoked_before=now).get(
        "/dashboard",
        headers={"Authorization": f"Bearer {_token('alice', 'primary-signing-secret-' * 2, issued_at=now - 1)}"},
    ).status_code == 401
    assert _client().get(
        "/dashboard",
        headers={"Authorization": f"Bearer {_token('alice', 'primary-signing-secret-' * 2, issuer='wrong')}"},
    ).status_code == 401
    assert _client().get(
        "/dashboard",
        headers={"Authorization": f"Bearer {_token('alice', 'primary-signing-secret-' * 2, audience='wrong')}"},
    ).status_code == 401
    assert _client().get(
        "/dashboard",
        headers={"Authorization": f"Bearer {_token('alice', 'primary-signing-secret-' * 2, expires_at=now + 3601)}"},
    ).status_code == 401


def test_local_role_key_cannot_authorize_production():
    client = _client(
        auth_viewer_key="v" * 32,
        auth_researcher_key="r" * 32,
        auth_operator_key="o" * 32,
        auth_admin_key="a" * 32,
    )
    assert client.get("/dashboard", headers={"Authorization": f"Bearer {'v' * 32}"}).status_code == 401


def test_production_configuration_rejects_shared_broker_identity_and_incomplete_live_boundary():
    config = Settings(
        _env_file=None,
        environment="production",
        auth_mode="production_identity",
        auth_identity_signing_secret="s" * 32,
        auth_identity_roles={
            "op": "operator", "review": "admin", "review2": "admin",
            "svc": "researcher", "work": "researcher", "sched": "researcher", "break": "operator",
        },
        auth_identity_principal_types={
            "op": "operator", "review": "reviewer", "review2": "reviewer",
            "svc": "service", "work": "worker", "sched": "scheduler", "break": "emergency",
        },
        auth_identity_issuer="issuer",
        auth_identity_audience="audience",
        paper_alpaca_api_key="same-key",
        paper_alpaca_api_secret="same-secret",
        live_alpaca_api_key="same-key",
        live_alpaca_api_secret="same-secret",
        paper_broker_account_id="same-account",
        live_broker_account_id="same-account",
        allow_live_trading=True,
    )
    blockers = config.validate_production_configuration()
    assert "paper and live broker credentials must be distinct" in blockers
    assert "paper and live broker account bindings must be distinct" in blockers
    assert "live trading requires the approved-live runtime environment" in blockers
    assert "live trading requires an explicit live broker" in blockers


def test_complete_production_identity_and_live_configuration_has_no_startup_blockers():
    config = Settings(
        _env_file=None,
        environment="approved-live",
        auth_mode="production_identity",
        auth_identity_signing_secret="s" * 32,
        auth_identity_roles={
            "op": "operator", "review": "admin", "review2": "admin",
            "svc": "researcher", "work": "researcher", "sched": "researcher",
            "break": "operator", "view": "viewer",
        },
        auth_identity_principal_types={
            "op": "operator", "review": "reviewer", "review2": "reviewer",
            "svc": "service", "work": "worker", "sched": "scheduler",
            "break": "emergency", "view": "viewer",
        },
        auth_identity_issuer="issuer",
        auth_identity_audience="audience",
        paper_alpaca_api_key="paper-key",
        paper_alpaca_api_secret="paper-secret",
        live_alpaca_api_key="live-key",
        live_alpaca_api_secret="live-secret",
        paper_broker_account_id="paper-account",
        live_broker_account_id="live-account",
        allow_live_trading=True,
        live_environment_name="approved-live",
        live_broker_name="alpaca_live",
    )
    assert config.validate_production_configuration() == []


def test_named_approved_live_environment_uses_production_identity_path():
    client = _client(environment="approved-live")
    assert client.get("/dashboard", headers=_headers("alice", key="approved-live")).status_code == 200
    assert client.get(
        "/dashboard",
        headers={"Authorization": f"Bearer {'v' * 32}"},
    ).status_code == 401


def test_clerk_gateway_accepts_only_fresh_signed_internal_identity():
    secret = "internal-clerk-gateway-secret-" * 2
    config = Settings(
        _env_file=None,
        environment="production",
        auth_mode="clerk_gateway",
        internal_auth_secret=secret,
        clerk_secret_key="sk_test_clerk",
        clerk_publishable_key="pk_test_clerk",
        tradier_api_key="paper-key",
        tradier_account_id="paper-account",
    )
    assert config.validate_production_configuration() == []

    app = FastAPI()
    app.add_middleware(AuthenticationMiddleware, configuration=config)
    app.add_api_route("/dashboard", lambda: {"ok": True}, methods=["GET"])
    client = TestClient(app)

    issued_at = str(int(time.time()))
    actor = "user_clerk"
    role = "viewer"
    signature = hmac.new(
        secret.encode(),
        f"{issued_at}\n{actor}\n{role}".encode(),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "X-Internal-Auth-Actor": actor,
        "X-Internal-Auth-Role": role,
        "X-Internal-Auth-Timestamp": issued_at,
        "X-Internal-Auth-Signature": signature,
    }
    assert client.get("/dashboard", headers=headers).status_code == 200
    assert client.get(
        "/dashboard",
        headers={**headers, "X-Internal-Auth-Role": "admin"},
    ).status_code == 401