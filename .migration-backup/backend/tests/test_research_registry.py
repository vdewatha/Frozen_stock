import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd
from alembic import command
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from app.db.schema import migration_config, assert_schema_current
from app.models.models import ResearchModelRun
from app.services.research_registry import register_research_run, validate_research_run
from app.services.research_training import train_research_run


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.engine = create_engine(f"sqlite:///{self.root / 'db.sqlite'}", connect_args={"timeout": 20})
        config = migration_config()
        config.set_main_option("sqlalchemy.url", str(self.engine.url))
        command.upgrade(config, "head")
        x = np.arange(400)
        data = pd.DataFrame({"date": pd.date_range("2020-01-01", periods=400), "close": 100 + np.sin(x) * 4, "volume": x + 1000})
        self.manifest = train_research_run(data, self.root / "runs", symbol="TEST", source="fixture")
        self.path = self.root / "runs" / self.manifest["run_id"]

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def write_manifest(self, manifest):
        (self.path / "manifest.json").write_text(json.dumps(manifest))

    def test_registration_idempotent_and_schema(self):
        assert_schema_current(self.engine)
        with Session(self.engine) as db, db.begin():
            first = register_research_run(db, self.path)
            self.assertEqual(first.id, register_research_run(db, self.path).id)
            self.assertEqual(first.status, "experimental")
            self.assertFalse(first.eligible_for_trading)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ResearchModelRun)), 1)

    def test_tamper_and_no_overwrite(self):
        with Session(self.engine) as db, db.begin():
            register_research_run(db, self.path)
        self.manifest["metrics"]["random_forest"]["brier_score"] = 0.12345
        self.write_manifest(self.manifest)
        with Session(self.engine) as db, self.assertRaisesRegex(ValueError, "overwritten"):
            register_research_run(db, self.path)
        (self.path / "dataset.csv").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "checksum"):
            validate_research_run(self.path)

    def test_missing_metadata_identity_and_live_state(self):
        for key in list(self.manifest):
            invalid = dict(self.manifest)
            del invalid[key]
            self.write_manifest(invalid)
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_research_run(self.path)
        for update in ({"run_id": "0" * 64}, {"status": "live"}, {"eligible_for_trading": True}):
            self.write_manifest(self.manifest | update)
            with self.assertRaises(ValueError):
                validate_research_run(self.path)

    def test_path_traversal_symlinks_and_extra_files(self):
        invalid = dict(self.manifest)
        invalid["files"] = dict(invalid["files"]) | {"../secret": "0" * 64}
        self.write_manifest(invalid)
        with self.assertRaises(ValueError):
            validate_research_run(self.path)
        self.write_manifest(self.manifest)
        link = self.root / "link"
        link.symlink_to(self.path, target_is_directory=True)
        with self.assertRaises(ValueError):
            validate_research_run(link)
        model = self.path / "random_forest.joblib"
        model.rename(self.root / "model")
        model.symlink_to(self.root / "model")
        with self.assertRaises(ValueError):
            validate_research_run(self.path)

    def test_concurrent_unique_registration(self):
        def register(_):
            with Session(self.engine) as db, db.begin():
                return register_research_run(db, self.path).id
        with ThreadPoolExecutor(max_workers=2) as workers:
            ids = list(workers.map(register, range(2)))
        self.assertEqual(ids[0], ids[1])
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ResearchModelRun)), 1)

    def test_caller_rollback_does_not_publish(self):
        with Session(self.engine) as db:
            register_research_run(db, self.path)
            db.rollback()
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ResearchModelRun)), 0)
