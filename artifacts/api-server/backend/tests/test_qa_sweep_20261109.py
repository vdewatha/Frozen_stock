"""Regressions for defects found by the 2026-11-09 deployed-app QA sweep."""
from __future__ import annotations

import ast
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core import response_cache
from app.core.config import Settings
from app.core.security import AuthenticationMiddleware


def _anonymous_client(client_host: str) -> TestClient:
    config = {f"auth_{role}_key": role.ljust(40, "-") for role in ("viewer", "researcher", "operator", "admin")}
    app = FastAPI()
    app.add_api_route("/dashboard", lambda: {"ok": True}, methods=["GET"])
    app.add_middleware(AuthenticationMiddleware, configuration=Settings(_env_file=None, allow_anonymous_viewer=True, **config))

    async def with_client(scope, receive, send):
        # Starlette 0.37's TestClient cannot set the peer address.
        if scope["type"] == "http":
            scope = dict(scope, client=(client_host, 50000))
        await app(scope, receive, send)

    return TestClient(with_client)


def test_anonymous_viewers_behind_the_gateway_get_separate_rate_buckets():
    client = _anonymous_client("127.0.0.1")
    for _ in range(120):
        assert client.get("/dashboard", headers={"x-internal-client-ip": "203.0.113.1"}).status_code == 200
    assert client.get("/dashboard", headers={"x-internal-client-ip": "203.0.113.1"}).status_code == 429
    # A different visitor is not throttled by the first one's traffic.
    assert client.get("/dashboard", headers={"x-internal-client-ip": "203.0.113.2"}).status_code == 200


def test_client_ip_header_is_ignored_unless_the_peer_is_the_local_gateway():
    client = _anonymous_client("198.51.100.7")
    for index in range(120):
        assert client.get("/dashboard", headers={"x-internal-client-ip": f"10.0.0.{index}"}).status_code == 200
    # Rotating the header from a non-loopback peer does not mint new buckets.
    assert client.get("/dashboard", headers={"x-internal-client-ip": "10.9.9.9"}).status_code == 429


def test_response_cache_is_a_pass_through_outside_production():
    calls = []
    with patch.object(response_cache.settings, "environment", "local"):
        response_cache.cached_read("k", 60, lambda: calls.append(1) or len(calls))
        assert response_cache.cached_read("k", 60, lambda: calls.append(1) or len(calls)) == 2


def test_response_cache_runs_one_computation_and_serves_stale_while_refreshing():
    response_cache.clear()
    with patch.object(response_cache.settings, "environment", "production"):
        assert response_cache.cached_read("slow", 60, lambda: "first") == "first"
        assert response_cache.cached_read("slow", 60, lambda: "unused") == "first"

        started, release = threading.Event(), threading.Event()

        def slow():
            started.set()
            release.wait(5)
            return "second"

        response_cache._entries["slow"] = (time.monotonic() - 120, "first")  # expire it
        worker = threading.Thread(target=lambda: response_cache.cached_read("slow", 60, slow))
        worker.start()
        assert started.wait(5)
        # While the refresh runs, other callers get the stale value at once.
        assert response_cache.cached_read("slow", 60, lambda: pytest.fail("second computation")) == "first"
        release.set()
        worker.join(5)
        assert response_cache.cached_read("slow", 60, lambda: "unused") == "second"
    response_cache.clear()


def test_learning_scope_job_skips_target_weight_rows_without_alerting():
    from app.tasks import jobs

    dispatch = MagicMock()
    db = MagicMock()
    with patch.object(jobs, "_run_job", side_effect=lambda name, work: work(db)), \
            patch.object(jobs, "_claim_learning_dispatch", return_value=(dispatch, "claimed")), \
            patch.object(jobs, "run_strategy_experiments") as experiments:
        result = jobs.strategy_learning_scope_job.run(symbol="KO", strategy_slug="passive_arm", dispatch_id=1)
    assert result["status"] == "skipped"
    experiments.assert_not_called()
    # Closed as done, so the batch recovery does not republish it.
    assert dispatch.status == "succeeded"


def test_news_summary_read_never_seeds_mock_articles():
    from app.api import routes

    with patch.object(routes, "summarize_news_context", return_value={}) as summarize:
        routes.news_summary("NOTAREALTICKER", db=MagicMock())
    assert summarize.call_args.kwargs["auto_seed"] is False


def test_intraday_status_for_unknown_symbol_is_404_not_500():
    from app.api import routes

    with patch.object(routes, "feed_status", side_effect=ValueError("ZZZZ is not in the trading universe")):
        with pytest.raises(HTTPException) as raised:
            routes.intraday_feed_status("ZZZZ", db=MagicMock())
    assert raised.value.status_code == 404


def test_latest_regime_with_too_little_history_is_409_not_500():
    from app.api import routes

    with patch("app.services.market_regime.latest_market_regime", side_effect=ValueError("At least 220 complete trusted observations are required")):
        with pytest.raises(HTTPException) as raised:
            routes.latest_regime(db=MagicMock())
    assert raised.value.status_code == 409


def test_scalp_write_routes_are_mounted_with_the_optional_routers():
    from app.api.scalp_research import router

    assert any(route.path == "/research/scalp/runs" and "POST" in route.methods for route in router.routes)
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    loader = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "load_optional_application_routes"
    )
    included = {
        element.id
        for node in ast.walk(loader) if isinstance(node, ast.For)
        for element in getattr(node.iter, "elts", []) if isinstance(element, ast.Name)
    }
    assert "scalp_research_router" in included


def test_stock_monitoring_snapshot_is_viewer_readable():
    from app.core.security import required_role

    assert required_role("GET", "/system/stock-monitoring") == "viewer"
    assert required_role("POST", "/system/stock-monitoring/run") == "operator"


def test_news_summary_ignores_synthetic_placeholders_and_never_seeds_them():
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    import app.models  # noqa: F401
    from app.db.base import Base
    from app.models import NewsArticle
    from app.services.news_sentiment import summarize_news_context

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        # No articles: nothing is invented.
        empty = summarize_news_context(db, "QQQ")
        assert empty["article_count"] == 0
        assert db.query(NewsArticle).count() == 0
        # Placeholders alone are not news; with a real article only it counts.
        db.add(NewsArticle(symbol="QQQ", published_at=datetime(2026, 11, 9, 12), source="mock_news",
                           title="Synthetic", summary="", sentiment_score=Decimal("0.9"), relevance_score=Decimal("1")))
        db.commit()
        assert summarize_news_context(db, "QQQ")["article_count"] == 0
        db.add(NewsArticle(symbol="QQQ", published_at=datetime(2026, 11, 9, 11), source="Reuters",
                           title="Real", summary="", sentiment_score=Decimal("-0.5"), relevance_score=Decimal("1")))
        db.commit()
        real = summarize_news_context(db, "QQQ")
        assert real["article_count"] == 1
        assert real["average_sentiment"] == -0.5
        assert real["synthetic_only"] is False
    engine.dispose()


def test_daily_news_import_never_stores_mock_headlines():
    from app.tasks import jobs

    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [MagicMock(symbol="SPY")]
    with patch.object(jobs, "_run_job", side_effect=lambda name, work: work(db)), \
            patch("app.services.news_sentiment.import_company_news",
                  return_value={"symbol": "SPY", "status": "unavailable", "rows_imported": 0}) as company, \
            patch("app.services.news_sentiment.import_mock_news") as mock_news:
        result = jobs.daily_news_import.run()
    assert company.call_args.kwargs["mock_fallback"] is False
    mock_news.assert_not_called()
    assert result["unavailable"] == ["SPY"]
