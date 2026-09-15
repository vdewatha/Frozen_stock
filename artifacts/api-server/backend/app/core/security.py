"""Fail-closed bearer authentication. Keys are deployment secrets, never cookies.

Unknown routes require admin until explicitly classified. Role credentials identify
service principals; deploy individual identity/OIDC before multi-user live access.
"""
from __future__ import annotations

from collections import OrderedDict
import base64
import hashlib
import hmac
import json
import logging
import re
import time
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings

logger = logging.getLogger("trading.security")
logger.setLevel(logging.INFO)
ROLES = {"viewer": 0, "researcher": 1, "operator": 2, "admin": 3}
ACTION_CONFIRMATION_HEADER = "x-action-confirmation"
IDEMPOTENCY_HEADER = "x-idempotency-key"
ACTION_REASON_HEADER = "x-action-reason"
READ_PATHS = {
    "/auth/session",
    "/crypto/status", "/crypto/bindings", "/crypto/decisions", "/crypto/shadow-audits",
    "/crypto/paper/account", "/crypto/paper/intents",
    "/crypto/paper/external-fills",
    "/crypto/paper/performance",
    "/assets", "/strategies", "/market-regimes", "/news", "/paper-trades",
    "/audit-logs", "/notifications", "/risk-rules",
    "/broker/status", "/models/performance", "/experiments", "/dashboard",
    "/economic/indicators", "/system/readiness", "/system/live-safety", "/system/live-operations", "/system/live-operations/evidence", "/system/live-pilot", "/system/deployment-monitor", "/system/operational-hardening", "/research/runs",
    "/stock/training/jobs", "/stock/training/binding", "/stock/learning-cycles",
    "/stock-paper/status",
    "/live-broker/status",
    "/stock/forward-trials", "/stock/forward-trials/bindings/eligible",
    "/trade-candidates/refresh-jobs/latest",
    "/market-data/{symbol}", "/news/{symbol}/summary", "/economic/context",
    "/learning/workers/{task_id}",
}
RESEARCH_POSTS = {
    "/crypto/collect",
    "/market-data/import", "/market-data/intraday/ingest", "/backtests", "/signals", "/models/predict",
    "/models/run", "/models/score-realized", "/news/import", "/economic/import",
    "/market-regimes/detect", "/trade-candidates/refresh-jobs",
    "/trade-candidates/decision-journal", "/trade-candidates/decision-scorecard/update-memory",
    "/trade-candidates/memory-replay/monitor", "/trade-candidates/activation-review",
    "/watchlist/import", "/experiments/run",
    "/stock/training/jobs",
    "/stock/learning-cycles",
}
OPERATOR_POSTS = {
    "/crypto/paper/intents", "/crypto/paper/kill-switch/enable",
    "/crypto/paper/recover",
    "/paper-trading/run-signal", "/paper-trading/reconcile", "/broker/paper/orders",
    "/safety/kill-switch/enable", "/safety/strategies/pause",
    "/portfolio/allocation-review-queue/review", "/portfolio/allocation-review-queue/dry-run",
    "/portfolio/allocation-plan/execute", "/portfolio/risk/actions",
    "/stock/training/binding",
    "/stock/learning-cycles",
    "/stock-paper/reconcile", "/stock-paper/halt", "/stock-paper/orders", "/stock-paper/signal",
}
ADMIN_POSTS = {
    "/system/deployment-monitor/run", "/system/operational-hardening/run", "/system/live-safety/transition", "/notifications/{notification_id}/acknowledge",
    "/notifications/{notification_id}/resolve", "/strategies/evaluate",
    "/strategies/reactivation-review", "/risk/settings", "/safety/kill-switch/disable",
    "/safety/strategies/resume", "/broker/live/orders",
    "/stock/training/jobs/recover",
    "/stock-paper/initialize", "/stock-paper/resume",
}


def required_role(method: str, path: str) -> str:
    if method == "GET" and re.fullmatch(r"/crypto/bindings/[1-9][0-9]*/evidence", path):
        return "viewer"
    if method == "POST" and re.fullmatch(r"/crypto/bindings/[1-9][0-9]*/observe", path):
        return "researcher"
    if method == "GET" and (
        re.fullmatch(r"/stock/training/jobs/[0-9a-f-]{36}", path)
        or re.fullmatch(r"/stock/training/runs/[0-9a-f]{64}/report", path)
        or re.fullmatch(r"/stock/training/models/[0-9a-f]{64}/lifecycle", path)
    ):
        return "viewer"
    if method == "POST" and re.fullmatch(r"/stock/training/jobs/[0-9a-f-]{36}/cancel", path):
        return "researcher"
    if method == "GET" and re.fullmatch(r"/stock/learning-cycles/[0-9a-f]{64}", path):
        return "viewer"
    if method == "GET" and re.fullmatch(r"/stock/learning-cycles/[0-9a-f]{64}/automatic-promotion", path):
        return "viewer"
    if method == "GET" and path == "/stock/learning-cycles/schedule-control":
        return "viewer"
    if method == "POST" and path == "/stock/learning-cycles":
        return "researcher"
    if method == "POST" and path == "/stock/learning-cycles/schedule-control":
        return "operator"
    if method == "POST" and re.fullmatch(r"/stock/learning-cycles/[0-9a-f]{64}/(review|action)", path):
        return "operator"
    if method == "POST" and re.fullmatch(r"/stock/training/models/[0-9a-f]{64}/lifecycle", path):
        return "operator"
    if method == "GET" and re.fullmatch(
        r"/stock/forward-trials/[0-9a-f-]{36}(?:/(decisions|metrics|promotion-readiness|preflight)"
        r"|/promotion-readiness/reports(?:/[1-9][0-9]*(?:/(download|session-evidence))?)?)?",
        path,
    ):
        return "viewer"
    if method == "POST" and re.fullmatch(r"/stock/forward-trials/[0-9a-f-]{36}/(start|pause|resume|stop)", path):
        return "operator"
    if method == "POST" and re.fullmatch(r"/crypto/paper/intents/[1-9][0-9]*/(dispatch|reconcile|abandon)", path):
        return "operator"
    if method == "GET" and path in READ_PATHS:
        return "viewer"
    if method == "GET" and path == "/stock-paper/recovery":
        return "viewer"
    if method == "GET" and path == "/stock-paper/recovery/evidence":
        return "operator"
    # Exactly one path parameter; the API validates its 64-character hex value.
    # Other nested research routes retain the default admin requirement.
    if method == "GET" and re.fullmatch(r"/research/runs/[^/]+", path):
        return "viewer"
    if method == "GET" and (
        re.fullmatch(r"/market-data/[^/]+", path)
        or re.fullmatch(r"/market-data/intraday/[^/]+(?:/status)?", path)
        or re.fullmatch(r"/news/[^/]+/summary", path)
        or re.fullmatch(r"/learning/workers/[^/]+", path)
    ):
        return "viewer"
    # These GETs can populate caches or persist derived state even without refresh.
    if method == "GET" and path in {
        "/trade-candidates", "/opportunity-radar", "/trade-candidates/decision-journal",
        "/trade-candidates/decision-scorecard", "/trade-candidates/memory-replay",
        "/trade-scorecard", "/strategies/governance-scorecard",
        "/strategies/reactivation-queue", "/strategies/improvement-queue",
        "/portfolio/allocation-plan", "/portfolio/allocation-review-queue",
    }:
        return "researcher"
    if method == "POST" and path in RESEARCH_POSTS:
        return "researcher"
    if method == "POST" and path == "/system/stock-monitoring/run":
        return "operator"
    if method == "POST" and path in {
        "/system/live-pilot/stop", "/system/live-pilot/rollback", "/system/live-pilot/review",
    }:
        return "operator"
    if method == "POST" and path in {
        "/system/live-pilot/activate", "/system/live-pilot/promote", "/system/live-pilot/limits",
    }:
        return "admin"
    if method == "POST" and (
        path == "/live-broker/reconcile" or path.startswith("/live-broker/orders")
    ):
        return "admin"
    if method == "POST" and path in {
        "/stock-paper/recovery/cancel",
        "/stock-paper/recovery/rollback",
        "/stock-paper/recovery/accounting-review",
    }:
        return "operator"
    if method == "POST" and (path in OPERATOR_POSTS or path.startswith((
        "/paper-trading/close/", "/paper-trading/reduce/", "/stock-paper/orders/", "/stock-paper/positions/",
    ))):
        return "operator"
    if method in {"POST", "PATCH"} and (
        path in ADMIN_POSTS
        or path.startswith("/notifications/")
        or path == "/risk/settings"
    ):
        return "admin"
    return "admin"


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _decode_identity_token(token: str, configuration, now: int) -> tuple[str, str, dict] | None:
    """Verify a compact HS256 identity assertion and map its subject server-side."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        header = json.loads(_b64decode(parts[0]))
        claims = json.loads(_b64decode(parts[1]))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if header.get("alg") != "HS256" or header.get("typ", "JWT") != "JWT":
        return None
    secrets = [
        configuration.auth_identity_signing_secret.get_secret_value(),
        configuration.auth_identity_previous_signing_secret.get_secret_value(),
    ]
    signing_input = f"{parts[0]}.{parts[1]}".encode()
    supplied = parts[2]
    valid_signature = any(
        secret and hmac.compare_digest(
            base64.urlsafe_b64encode(
                hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
            ).rstrip(b"=").decode(),
            supplied,
        )
        for secret in secrets
    )
    if not valid_signature:
        return None
    subject = str(claims.get("sub", "")).strip()
    role = configuration.auth_identity_roles.get(subject)
    # The role claim is not trusted. The server-side mapping is authoritative.
    if not subject or role not in ROLES:
        return None
    if claims.get("iss") and configuration.auth_identity_issuer and claims["iss"] != configuration.auth_identity_issuer:
        return None
    if claims.get("aud") and configuration.auth_identity_audience:
        audience = claims["aud"]
        audiences = [audience] if isinstance(audience, str) else audience
        if configuration.auth_identity_audience not in audiences:
            return None
    try:
        if int(claims.get("exp", 0)) <= now:
            return None
        if int(claims.get("iat", now)) < configuration.auth_identity_revoked_before:
            return None
    except (TypeError, ValueError):
        return None
    return subject, role, {
        "method": "identity_assertion",
        "subject": subject,
        "role": role,
        "issuer": claims.get("iss") or None,
        "key_rotated": bool(secrets[1]),
    }


def authorization_evidence(request: Request) -> dict:
    """Return safe, durable authorization metadata for audit payloads."""
    idempotency = request.headers.get(IDEMPOTENCY_HEADER, "").strip()
    return {
        "actor": getattr(request.state, "actor", "unknown"),
        "role": getattr(request.state, "auth_role", "unknown"),
        "request_id": getattr(request.state, "request_id", None),
        "auth_method": (getattr(request.state, "auth_evidence", {}) or {}).get("method", "unknown"),
        "confirmation": request.headers.get(ACTION_CONFIRMATION_HEADER, "").lower() == "confirm",
        "idempotency_key_sha256": hashlib.sha256(idempotency.encode()).hexdigest() if idempotency else None,
    }


class AuthenticationMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, configuration=None):
        super().__init__(app)
        self.configuration = configuration or settings
        self.windows = OrderedDict()
        self.replayed_requests: dict[tuple[str, str, str], str] = {}

    def _production_blockers(self) -> list[str]:
        validator = getattr(self.configuration, "validate_production_configuration", None)
        return validator() if validator else []

    def _intent_blocker(self, request: Request, required: str) -> str | None:
        if self.configuration.environment != "production" or ROLES[required] < ROLES["operator"]:
            return None
        if request.headers.get(ACTION_CONFIRMATION_HEADER, "").lower() != "confirm":
            return "explicit action confirmation is required"
        if not request.headers.get(IDEMPOTENCY_HEADER, "").strip():
            return "an idempotency key is required"
        if not request.headers.get(ACTION_REASON_HEADER, "").strip():
            return "an action reason is required"
        return None

    def _check_replay(self, request: Request, actor: str) -> str | None:
        key = request.headers.get(IDEMPOTENCY_HEADER, "").strip()
        if not key or self.configuration.environment != "production":
            return None
        fingerprint = hashlib.sha256(
            f"{request.method}:{request.url.path}:{request.url.query}".encode()
        ).hexdigest()
        replay_key = (actor, key, request.method)
        if replay_key in self.replayed_requests:
            return "request idempotency key has already been used"
        self.replayed_requests[replay_key] = fingerprint
        if len(self.replayed_requests) > 8192:
            self.replayed_requests.pop(next(iter(self.replayed_requests)))
        return None

    def _limited(self, identity: str) -> bool:
        now = time.monotonic()
        start, count = self.windows.pop(identity, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        self.windows[identity] = (start, count + 1)
        if len(self.windows) > 4096:
            self.windows.popitem(last=False)
        return count >= 120

    async def dispatch(self, request: Request, call_next):
        path = request.url.path.removeprefix("/api") or "/"
        if request.method == "GET" and path in {"/health", "/ready", "/auth/config"}:
            return await call_next(request)
        correlation = str(uuid4())
        required = required_role(request.method, path)
        actor = "anonymous"
        auth_role = "anonymous"
        auth_evidence: dict = {"method": "none"}
        status = 500
        try:
            token = request.headers.get("authorization", "")
            candidate = token[7:] if token.lower().startswith("bearer ") else ""
            if self.configuration.environment == "production":
                blockers = self._production_blockers()
                if blockers:
                    status = 503
                    return JSONResponse(
                        {"detail": "Production authentication is not configured", "request_id": correlation},
                        status_code=status,
                    )
                verified = _decode_identity_token(candidate, self.configuration, int(time.time()))
                if verified:
                    actor, auth_role, auth_evidence = verified
            else:
                configured = [(role, getattr(self.configuration, f"auth_{role}_key").get_secret_value()) for role in ROLES]
                keys = [key for _, key in configured]
                # Every local environment is fail-closed: each role has its own secret.
                if any(len(key) < 32 for key in keys) or len(set(keys)) != len(keys):
                    status = 503
                    return JSONResponse({"detail": "Authentication is not configured", "request_id": correlation}, status_code=status)
                for role, key in configured:
                    if key and hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(), hashlib.sha256(key.encode()).digest()):
                        actor, auth_role = role, role
                        auth_evidence = {"method": "local_role_key", "role": role}
            identity = actor if actor != "anonymous" else (request.client.host if request.client else "unknown")
            if self._limited(identity):
                status = 429
                return JSONResponse({"detail": "Request rate exceeded"}, status_code=status, headers={"Retry-After": "60"})
            if actor == "anonymous":
                status = 401
                return JSONResponse({"detail": "Authenticated identity required"}, status_code=status, headers={"WWW-Authenticate": "Bearer"})
            if ROLES[auth_role] < ROLES[required]:
                status = 403
                return JSONResponse({"detail": "Insufficient role"}, status_code=status)
            intent_error = self._intent_blocker(request, required)
            if intent_error:
                status = 428
                return JSONResponse({"detail": intent_error, "request_id": correlation}, status_code=status)
            replay_error = self._check_replay(request, actor)
            if replay_error:
                status = 409
                return JSONResponse({"detail": replay_error, "request_id": correlation}, status_code=status)
            request.state.actor = actor
            request.state.auth_role = auth_role
            request.state.auth_evidence = auth_evidence
            request.state.request_id = correlation
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = correlation
            return response
        finally:
            # Never log authorization, bodies, URLs, query parameters, or free text.
            logger.info(json.dumps({"event": "api_access", "actor": actor, "request_id": correlation,
                                    "role": auth_role, "required_role": required,
                                    "auth_method": auth_evidence.get("method"),
                                    "reason": "role_policy", "result": status}))
