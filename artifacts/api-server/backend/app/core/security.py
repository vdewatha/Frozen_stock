"""Fail-closed bearer authentication. Keys are deployment secrets, never cookies.

Unknown routes require admin until explicitly classified. Role credentials identify
service principals; deploy individual identity/OIDC before multi-user live access.
"""
from __future__ import annotations

from collections import OrderedDict
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
READ_PATHS = {
    "/auth/session",
    "/crypto/status", "/crypto/bindings", "/crypto/decisions", "/crypto/shadow-audits",
    "/crypto/paper/account", "/crypto/paper/intents",
    "/crypto/paper/external-fills",
    "/crypto/paper/performance",
    "/assets", "/strategies", "/market-regimes", "/news", "/paper-trades",
    "/audit-logs", "/notifications", "/risk-rules",
    "/broker/status", "/models/performance", "/experiments", "/dashboard",
    "/economic/indicators", "/system/readiness", "/system/deployment-monitor", "/research/runs",
    "/stock/training/jobs", "/stock/training/binding",
    "/stock-paper/status",
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
}
OPERATOR_POSTS = {
    "/crypto/paper/intents", "/crypto/paper/kill-switch/enable",
    "/crypto/paper/recover",
    "/paper-trading/run-signal", "/paper-trading/reconcile", "/broker/paper/orders",
    "/safety/kill-switch/enable", "/safety/strategies/pause",
    "/portfolio/allocation-review-queue/review", "/portfolio/allocation-review-queue/dry-run",
    "/portfolio/allocation-plan/execute", "/portfolio/risk/actions",
    "/stock/training/binding",
    "/stock-paper/reconcile", "/stock-paper/halt", "/stock-paper/orders", "/stock-paper/signal",
}
ADMIN_POSTS = {
    "/system/deployment-monitor/run", "/notifications/{notification_id}/acknowledge",
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
    ):
        return "viewer"
    if method == "POST" and re.fullmatch(r"/stock/training/jobs/[0-9a-f-]{36}/cancel", path):
        return "researcher"
    if method == "GET" and re.fullmatch(r"/stock/forward-trials/[0-9a-f-]{36}(?:/(decisions|metrics|promotion-readiness))?", path):
        return "viewer"
    if method == "POST" and re.fullmatch(r"/stock/forward-trials/[0-9a-f-]{36}/(start|pause|resume|stop)", path):
        return "operator"
    if method == "POST" and re.fullmatch(r"/crypto/paper/intents/[1-9][0-9]*/(dispatch|reconcile|abandon)", path):
        return "operator"
    if method == "GET" and path in READ_PATHS:
        return "viewer"
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


class AuthenticationMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, configuration=None):
        super().__init__(app)
        self.configuration = configuration or settings
        self.windows = OrderedDict()

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
        if request.method == "GET" and path == "/health":
            return await call_next(request)
        correlation = str(uuid4())
        required = required_role(request.method, path)
        actor = "anonymous"
        status = 500
        try:
            configured = [(role, getattr(self.configuration, f"auth_{role}_key").get_secret_value()) for role in ROLES]
            keys = [key for _, key in configured]
            # Every environment is fail-closed: each role has its own secret.
            if any(len(key) < 32 for key in keys) or len(set(keys)) != len(keys):
                status = 503
                return JSONResponse({"detail": "Authentication is not configured", "request_id": correlation}, status_code=status)
            token = request.headers.get("authorization", "")
            candidate = token[7:] if token.lower().startswith("bearer ") else ""
            for role, key in configured:
                if key and hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(), hashlib.sha256(key.encode()).digest()):
                    actor = role
            identity = actor if actor != "anonymous" else (request.client.host if request.client else "unknown")
            if self._limited(identity):
                status = 429
                return JSONResponse({"detail": "Request rate exceeded"}, status_code=status, headers={"Retry-After": "60"})
            if actor == "anonymous":
                status = 401
                return JSONResponse({"detail": "Bearer authentication required"}, status_code=status, headers={"WWW-Authenticate": "Bearer"})
            if ROLES[actor] < ROLES[required]:
                status = 403
                return JSONResponse({"detail": "Insufficient role"}, status_code=status)
            request.state.actor = actor
            request.state.request_id = correlation
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = correlation
            return response
        finally:
            # Never log authorization, bodies, URLs, query parameters, or free text.
            logger.info(json.dumps({"event": "api_access", "actor": actor, "request_id": correlation,
                                    "required_role": required, "reason": "role_policy", "result": status}))
