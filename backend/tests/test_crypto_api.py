import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from alembic import command
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.crypto_research import router
from app.api.shadow_evidence import router as evidence_router
from app.core.config import Settings
from app.core.security import AuthenticationMiddleware
from app.db.schema import migration_config, assert_schema_current
from app.db.session import get_db


class CryptoApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.temp.name) / 'api.db'}", connect_args={"check_same_thread": False})
        config = migration_config()
        config.set_main_option("sqlalchemy.url", str(self.engine.url))
        command.upgrade(config, "head")
        assert_schema_current(self.engine)
        app = FastAPI()
        app.add_middleware(AuthenticationMiddleware, configuration=Settings(_env_file=None,
            auth_viewer_key="v" * 32, auth_researcher_key="r" * 32,
            auth_operator_key="o" * 32, auth_admin_key="a" * 32))
        app.include_router(router)
        app.include_router(evidence_router)
        def session():
            with Session(self.engine) as db:
                yield db
        app.dependency_overrides[get_db] = session
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.temp.cleanup()

    def call(self, method, path, role="v", **kwargs):
        return getattr(self.client, method)("/crypto" + path, headers={"Authorization": "Bearer " + role * 32}, **kwargs)

    def test_read_only_and_role_boundaries(self):
        for path in ("/status", "/bindings", "/decisions", "/shadow-audits", "/paper/account", "/paper/intents"):
            self.assertEqual(self.client.get("/crypto" + path).status_code, 401)
            self.assertEqual(self.call("get", path).status_code, 200)
        for path in ("/collect", "/bindings", "/paper/account", "/paper/trials", "/paper/intents", "/paper/kill-switch/enable"):
            self.assertEqual(self.call("post", path, json={}).status_code, 403)
        for path in ("/paper/account", "/paper/trials", "/paper/kill-switch/disable"):
            self.assertEqual(self.call("post", path, "o", json={}).status_code, 403)

    def test_account_defaults_and_reset_rejected(self):
        self.assertEqual(self.call("post", "/paper/account", "a", json={"starting_cash": "10000"}).status_code, 200)
        self.assertTrue(self.call("get", "/paper/account").json()["kill_switch"])
        self.assertEqual(self.call("post", "/paper/account", "a", json={"starting_cash": "20000"}).status_code, 409)
        self.assertEqual(self.call("post", "/paper/kill-switch/disable", "a").status_code, 200)
        self.assertFalse(self.call("get", "/paper/account").json()["kill_switch"])
        self.assertEqual(self.call("post", "/paper/kill-switch/enable", "o").status_code, 200)
        self.assertTrue(self.call("get", "/paper/account").json()["kill_switch"])

    def test_unknown_fields_and_invalid_bindings_rejected(self):
        self.assertEqual(self.call("post", "/paper/account", "a", json={"starting_cash": "1", "live": True}).status_code, 422)
        self.assertEqual(self.call("post", "/bindings", "a", json={"run_id": "0" * 64, "spec": {}}).status_code, 409)
        self.assertEqual(self.call("post", "/bindings/1/observe", "r").status_code, 409)
        self.assertEqual(self.call("get", "/decisions?limit=101").status_code, 422)

    def test_execution_disabled_without_network(self):
        with patch("app.api.crypto_research.FreqtradeDryRunClient", side_effect=AssertionError("network forbidden")), \
             patch("app.api.crypto_research.settings.freqtrade_paper_execution_enabled", False):
            for action in ("dispatch", "reconcile"):
                self.assertEqual(self.call("post", f"/paper/intents/1/{action}", "o", json={}).status_code, 409)
                self.assertEqual(self.call("post", f"/paper/intents/1/{action}", "r", json={}).status_code, 403)
            self.assertEqual(self.call("post", "/paper/recover", "o").status_code, 409)
            self.assertEqual(self.call("post", "/paper/recover", "v").status_code, 403)

    def test_forward_reports_are_read_only_viewer_routes(self):
        self.assertEqual(self.call("get", "/bindings/1/evidence").status_code, 404)
        self.assertEqual(self.call("get", "/paper/external-fills").status_code, 200)
        report = self.call("get", "/paper/performance?approval_id=1")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["positive_trades"], 0)
        self.assertFalse(report.json()["live_authorized"])
