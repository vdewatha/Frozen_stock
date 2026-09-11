import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import AuthenticationMiddleware, required_role


class SecurityTests(unittest.TestCase):
    def test_required_role_frontend_route_matrix(self):
        cases = {
            ("GET", "/dashboard"): "viewer",
            ("GET", "/auth/session"): "viewer",
            ("GET", "/market-data/SPY"): "viewer",
            ("GET", "/news/SPY/summary"): "viewer",
            ("GET", "/learning/workers/abc-123"): "viewer",
            ("GET", "/trade-candidates"): "researcher",
            ("GET", "/trade-candidates/decision-journal"): "researcher",
            ("GET", "/opportunity-radar"): "researcher",
            ("GET", "/portfolio/allocation-plan"): "researcher",
            ("POST", "/market-data/import"): "researcher",
            ("POST", "/models/run"): "researcher",
            ("POST", "/trade-candidates/activation-review"): "researcher",
            ("POST", "/paper-trading/run-signal"): "operator",
            ("POST", "/paper-trading/close/42"): "operator",
            ("POST", "/portfolio/allocation-plan/execute"): "operator",
            ("POST", "/safety/kill-switch/enable"): "operator",
            ("POST", "/risk/settings"): "admin",
            ("PATCH", "/risk/settings"): "admin",
            ("POST", "/safety/kill-switch/disable"): "admin",
            ("POST", "/safety/strategies/resume"): "admin",
            ("POST", "/notifications/7/acknowledge"): "admin",
            ("POST", "/system/deployment-monitor/run"): "admin",
            ("POST", "/broker/live/orders"): "admin",
            ("GET", "/unclassified/frontend-route"): "admin",
        }
        for (method, path), expected in cases.items():
            with self.subTest(method=method, path=path):
                self.assertEqual(required_role(method, path), expected)

    def test_manual_broker_cannot_bypass_risk(self):
        from app.api.routes import submit_manual_paper_order
        from fastapi import HTTPException
        with patch("app.api.routes.submit_paper_order") as submit:
            with self.assertRaises(HTTPException) as caught:
                submit_manual_paper_order(MagicMock(), MagicMock())
        self.assertEqual(caught.exception.status_code, 409)
        submit.assert_not_called()

    def test_broker_rejects_nonfinite_quantity(self):
        from app.services.broker import submit_paper_order
        for quantity in (float("nan"), float("inf"), 0, -1):
            db = MagicMock()
            with self.assertRaises(ValueError):
                submit_paper_order(db, symbol="SPY", side="buy", quantity=quantity)
            db.add.assert_not_called()

    def make_client(self, **overrides):
        config = {f"auth_{role}_key": role.ljust(40, "-") for role in ("viewer", "researcher", "operator", "admin")}
        config.update(overrides)
        app = FastAPI()
        app.add_middleware(AuthenticationMiddleware, configuration=Settings(_env_file=None, **config))
        for path, method in [("/health", "GET"), ("/dashboard", "GET"), ("/auth/session", "GET"), ("/trade-candidates", "GET"),
                             ("/models/run", "POST"), ("/paper-trading/run-signal", "POST"),
                             ("/safety/kill-switch/disable", "POST"), ("/new-route", "GET")]:
            app.add_api_route(path, lambda: {"ok": True}, methods=[method])
        return TestClient(app)

    def headers(self, role):
        return {"Authorization": "Bearer " + role.ljust(40, "-")}

    def test_anonymous_and_invalid_credentials(self):
        client = self.make_client()
        self.assertEqual(client.get("/health").status_code, 200)
        for headers in ({}, {"Authorization": "Bearer bogus"}):
            self.assertEqual(client.get("/dashboard", headers=headers).status_code, 401)
            self.assertEqual(client.post("/models/run", headers=headers).status_code, 401)

    def test_each_role_matrix(self):
        paths = [("GET", "/dashboard", 0), ("GET", "/trade-candidates?refresh=true", 1),
                 ("POST", "/models/run", 1), ("POST", "/paper-trading/run-signal", 2),
                 ("POST", "/safety/kill-switch/disable", 3), ("GET", "/new-route", 3)]
        for level, role in enumerate(("viewer", "researcher", "operator", "admin")):
            client = self.make_client()
            for method, path, required in paths:
                with self.subTest(role=role, path=path):
                    response = client.request(method, path, headers=self.headers(role))
                    self.assertEqual(response.status_code, 200 if level >= required else 403)

    def test_authenticated_role_is_available_without_credential_material(self):
        from starlette.requests import Request

        app = FastAPI()
        config = {f"auth_{role}_key": role.ljust(40, "-") for role in ("viewer", "researcher", "operator", "admin")}
        app.add_middleware(AuthenticationMiddleware, configuration=Settings(_env_file=None, **config))

        @app.get("/auth/session")
        def session(request: Request):
            return {"role": request.state.actor}

        client = TestClient(app)
        for role in ("viewer", "researcher", "operator", "admin"):
            response = client.get("/auth/session", headers=self.headers(role))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"role": role})
            self.assertNotIn(role.ljust(40, "-"), response.text)

    def test_configuration_fails_closed(self):
        for config in ({"auth_admin_key": "short"}, {"auth_admin_key": "viewer".ljust(40, "-")}):
            client = self.make_client(**config)
            self.assertEqual(client.get("/dashboard", headers=self.headers("viewer")).status_code, 503)
            self.assertEqual(client.get("/health").status_code, 200)

    def test_no_secret_in_audit(self):
        client = self.make_client()
        with self.assertLogs("trading.security", level="INFO") as logs:
            response = client.post("/safety/kill-switch/disable", headers=self.headers("admin"), json={"reason": "private"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("X-Request-ID", response.headers)
        self.assertNotIn("admin".ljust(40, "-"), str(logs.output))
        self.assertNotIn("private", str(logs.output))
        self.assertIn('"actor": "admin"', str(logs.output))

    def test_rate_limit(self):
        client = self.make_client()
        for _ in range(120):
            self.assertEqual(client.get("/dashboard", headers=self.headers("viewer")).status_code, 200)
        self.assertEqual(client.get("/dashboard", headers=self.headers("viewer")).status_code, 429)


if __name__ == "__main__":
    unittest.main()
