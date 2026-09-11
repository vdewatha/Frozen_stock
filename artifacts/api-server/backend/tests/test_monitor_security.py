import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

import httpx

from app.services import deployment_monitor

spec = importlib.util.spec_from_file_location("monitor_cli", Path(__file__).parents[1] / "scripts" / "check_deployment_monitor.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class MonitorSecurityTests(unittest.TestCase):
    def test_schedule_definition_returns_explicit_result(self):
        result = deployment_monitor._check_celery_schedule()
        self.assertIn("configured_jobs", result["details"])
        self.assertIn("missing_required_jobs", result["details"])
        self.assertTrue(result["details"]["configured_jobs"])
        self.assertEqual(result["status"], "ready")

    def test_redis_distinguishes_configuration_and_reachability(self):
        with patch.object(deployment_monitor.settings, "redis_url", ""):
            result = deployment_monitor._check_redis()
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["details"]["configured"])
        self.assertFalse(result["details"]["reachable"])

        with patch.dict("os.environ", {"REDIS_URL": "redis://unreachable"}), patch.object(
            deployment_monitor.settings, "redis_url", "redis://unreachable"
        ), patch.object(
            deployment_monitor.redis.Redis,
            "from_url",
            side_effect=ConnectionError("unreachable"),
        ):
            result = deployment_monitor._check_redis()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["details"]["configured"])
        self.assertFalse(result["details"]["reachable"])

    def test_worker_evidence_requires_a_ping_response(self):
        inspector = MagicMock()
        with patch.object(deployment_monitor.celery_app.control, "inspect", return_value=inspector):
            for responses, healthy in (({}, False), ({"worker-a": {"ok": "pong"}}, True), ({"worker-a": {}, "worker-b": {}}, True)):
                with self.subTest(responses=responses):
                    inspector.ping.return_value = responses
                    result = deployment_monitor._check_celery_workers()
                    self.assertEqual(result["status"] == "ready", healthy)
                    self.assertEqual(result["details"]["worker_count"], len(responses))

            inspector.ping.side_effect = RuntimeError("worker unavailable")
            result = deployment_monitor._check_celery_workers()
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["details"]["workers"], [])

    def test_scheduler_evidence_requires_exactly_one_heartbeat(self):
        client = MagicMock()
        with patch.object(deployment_monitor.redis.Redis, "from_url", return_value=client):
            for evidence, healthy in (([], False), (["celery:beat:lease:one"], True), (["celery:beat:lease:one", "celery:beat:lease:two"], False)):
                with self.subTest(evidence=evidence):
                    client.scan_iter.return_value = evidence
                    result = deployment_monitor._check_celery_beat()
                    self.assertEqual(result["status"] == "ready", healthy)
                    self.assertEqual(result["details"]["count"], len(evidence))

    def test_real_viewer_monitor_route_never_creates_risk_settings(self):
        import tempfile
        from alembic import command
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine, select, func
        from sqlalchemy.orm import Session
        from app.api.routes import router
        from app.core.config import Settings
        from app.core.security import AuthenticationMiddleware
        from app.db.session import get_db
        from app.db.schema import migration_config
        from app.models import RiskRule
        with tempfile.TemporaryDirectory() as root:
            engine = create_engine(f"sqlite:///{root}/monitor.db", connect_args={"check_same_thread": False})
            config = migration_config()
            config.set_main_option("sqlalchemy.url", str(engine.url))
            command.upgrade(config, "head")
            app = FastAPI()
            app.add_middleware(
                AuthenticationMiddleware,
                configuration=Settings(
                    _env_file=None,
                    auth_viewer_key="v" * 32,
                    auth_researcher_key="r" * 32,
                    auth_operator_key="o" * 32,
                    auth_admin_key="a" * 32,
                ),
            )
            app.include_router(router)
            def session():
                with Session(engine) as db:
                    yield db
            app.dependency_overrides[get_db] = session
            with TestClient(app) as client, patch.object(deployment_monitor, "_check_redis", return_value={"name": "Redis", "status": "ready", "message": "test", "details": {}}):
                self.assertEqual(client.get("/system/deployment-monitor").status_code, 401)
                response = client.get("/system/deployment-monitor", headers={"Authorization": "Bearer " + "v" * 32})
                self.assertEqual(response.status_code, 200, response.text)
                snapshot = response.json()
                self.assertFalse(snapshot["paper_trading_allowed"])
                risk = next(c for c in snapshot["readiness"]["checks"] if c["name"] == "Risk state")
                self.assertEqual(risk["status"], "blocked")
                self.assertFalse(risk["details"]["risk_rule_present"])
                self.assertEqual(client.post("/system/deployment-monitor/run", headers={"Authorization": "Bearer " + "v" * 32}).status_code, 403)
            with Session(engine) as db:
                self.assertEqual(db.scalar(select(func.count()).select_from(RiskRule)), 0)
            engine.dispose()

    def test_authenticated_nonredirecting_transport(self):
        client = MagicMock()
        client.get.return_value = httpx.Response(200, json={"status": "blocked"}, request=httpx.Request("GET", "https://example.com"))
        with patch.dict("os.environ", {"AUTH_VIEWER_KEY": "v" * 32}), patch.object(monitor.httpx, "Client") as factory:
            factory.return_value.__enter__.return_value = client
            self.assertEqual(monitor._load_snapshot("https://example.com", 5), {"status": "blocked"})
            factory.assert_called_once_with(timeout=5, follow_redirects=False, trust_env=False)
            self.assertEqual(client.get.call_args.kwargs["headers"]["Authorization"], "Bearer " + "v" * 32)
            client.get.return_value = httpx.Response(302, headers={"location": "https://evil.example"}, request=httpx.Request("GET", "https://example.com"))
            with self.assertRaises(httpx.HTTPStatusError):
                monitor._load_snapshot("https://example.com", 5)

    def test_invalid_inputs_fail_before_network(self):
        with patch.dict("os.environ", {"AUTH_VIEWER_KEY": "v" * 32}), patch.object(monitor.httpx, "Client") as factory:
            for url in ("http://example.com", "https://u:secret@example.com", "https://example.com?token=secret", "https://example.com/path"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    monitor._load_snapshot(url, 5)
            with self.assertRaises(ValueError):
                monitor._load_snapshot("https://example.com", float("nan"))
            factory.assert_not_called()

    def test_infrastructure_errors_do_not_expose_secrets(self):
        db = MagicMock()
        db.execute.side_effect = RuntimeError("database-password")
        self.assertNotIn("database-password", str(deployment_monitor._check_database(db)))
        with patch.object(deployment_monitor.redis.Redis, "from_url", side_effect=RuntimeError("redis-password")):
            result = deployment_monitor._check_redis()
        self.assertNotIn("redis-password", str(result))
        self.assertNotIn("redis_url", result["details"])
