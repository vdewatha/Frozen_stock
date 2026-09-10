from pathlib import Path
import tempfile
import unittest

from alembic import command
from fastapi import FastAPI
from fastapi.testclient import TestClient
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.api.research import router
from app.core.config import Settings
from app.core.security import AuthenticationMiddleware
from app.db.schema import migration_config
from app.db.session import get_db
from app.services.research_registry import register_research_run
from app.services.research_training import train_research_run


class ResearchApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name).resolve()
        cls.engine = create_engine(f"sqlite:///{cls.root / 'api.db'}", connect_args={"check_same_thread": False})
        config = migration_config()
        config.set_main_option("sqlalchemy.url", str(cls.engine.url))
        command.upgrade(config, "head")
        x = np.arange(400)
        prices = pd.DataFrame({"date": pd.date_range("2020-01-01", periods=400), "close": 100 + np.sin(x) * 4, "volume": x + 1000})
        manifest = train_research_run(prices, cls.root / "runs", symbol="TEST", source="fixture")
        cls.run_id = manifest["run_id"]
        with Session(cls.engine) as db, db.begin():
            register_research_run(db, cls.root / "runs" / cls.run_id)
        app = FastAPI()
        app.add_middleware(AuthenticationMiddleware, configuration=Settings(_env_file=None, auth_viewer_key="v" * 32))
        app.include_router(router)
        def session():
            with Session(cls.engine) as db:
                yield db
        app.dependency_overrides[get_db] = session
        cls.client = TestClient(app)
        cls.headers = {"Authorization": "Bearer " + "v" * 32}

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.engine.dispose()
        cls.temp.cleanup()

    def test_authenticated_summaries_do_not_expose_paths_or_write(self):
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            for endpoint in ("/research/runs", f"/research/runs/{self.run_id}"):
                self.assertEqual(self.client.get(endpoint).status_code, 401)
                response = self.client.get(endpoint, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(str(self.root), response.text)
                for private in ("artifact_path", "source_claim", "files", "code_sha256", "training_metadata", ".joblib"):
                    self.assertNotIn(private, response.text)
            detail = response.json()
            self.assertEqual(detail["status"], "experimental")
            self.assertFalse(detail["eligible_for_trading"])
            self.assertIn("training_prevalence_baseline", detail["metrics"])
            self.assertIn("platform", detail["versions"])
            self.assertTrue(statements)
            self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in statements))
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

    def test_pagination_and_validation(self):
        page = self.client.get("/research/runs?limit=1", headers=self.headers).json()
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["limit"], 1)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(self.client.get("/research/runs?offset=1", headers=self.headers).json()["items"], [])
        for query in ("limit=0", "limit=101", "limit=no", "offset=-1"):
            self.assertEqual(self.client.get("/research/runs?" + query, headers=self.headers).status_code, 422)
        self.assertEqual(self.client.get("/research/runs/invalid", headers=self.headers).status_code, 422)
        self.assertEqual(self.client.get("/research/runs/" + "0" * 64, headers=self.headers).status_code, 404)

    def test_no_mutation_or_nested_route_permission(self):
        self.assertEqual(self.client.post("/research/runs", headers=self.headers, json={}).status_code, 403)
        self.assertEqual(self.client.get(f"/research/runs/{self.run_id}/files", headers=self.headers).status_code, 403)
