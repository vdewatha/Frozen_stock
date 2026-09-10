import copy
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.db.base import Base
from app.models.models import ResearchModelRun
from app.models.shadow import ShadowDecision, ShadowRunAudit
from app.services.shadow_pipeline import (INSTRUMENT, NAMES, canonical, feature_config_id,
    register_shadow_model, run_shadow, score_shadow)


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.clock = datetime(2025, 1, 10, 12, 1, tzinfo=timezone.utc)
        self.spec = dict(version=1, run_id="a"*64, feature_config_id=feature_config_id(), feature_names=NAMES,
                         mean=[0.]*10, scale=[1.]*10, coef=[.01]*10, intercept=0.,
                         training_cutoff="2024-01-01T00:00:00+00:00", instrument=INSTRUMENT,
                         timeframe_minutes=60, horizon_bars=1)
        self.meta = dict(instrument_id=INSTRUMENT,timeframe_minutes=60,feature_config_id=feature_config_id(),
                         horizon_bars=1,train_label_end=self.spec["training_cutoff"],
                         files={"logistic_regression.json":hashlib.sha256(canonical(self.spec)).hexdigest()})
        self.run = ResearchModelRun(run_id=self.spec["run_id"], manifest_sha256=hashlib.sha256(canonical(self.meta)).hexdigest(),
                     artifact_path="/not-loaded", training_metadata=self.meta, status="experimental",eligible_for_trading=False)
        self.db.add(self.run)
        self.db.flush()
        self.rows = [SimpleNamespace(opened_at=(self.clock.replace(minute=0)-timedelta(hours=100-i)).isoformat(),
             close=100+i*.1+(-1)**i, volume=1000+i, content_sha256=str(i)) for i in range(100)]

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def binding(self):
        return register_shadow_model(self.db,self.run.run_id,self.spec,"operator:test")

    def test_no_executable_loader_and_idempotent(self):
        binding = self.binding()
        self.assertEqual(binding.id,self.binding().id)
        with patch("app.services.shadow_pipeline.load_closed_history",return_value=self.rows), patch("joblib.load",side_effect=AssertionError("unsafe")):
            first=run_shadow(self.db,binding.id,self.clock)
            second=run_shadow(self.db,binding.id,self.clock)
        self.assertEqual(first.id,second.id)
        self.assertFalse(first.backfilled)
        self.assertFalse(first.eligible_for_qualification)
        self.assertEqual(first.bar_close, self.clock.replace(minute=0,tzinfo=None))

    def test_unsafe_and_tampered_spec(self):
        for key,value in (("scale",[0.]*10),("coef",[float("nan")]*10),("instrument","AAPL"),("version",True)):
            with self.subTest(key=key):
                spec=copy.deepcopy(self.spec); spec[key]=value
                with self.assertRaises(ValueError): register_shadow_model(self.db,self.run.run_id,spec,"test")
        spec=copy.deepcopy(self.spec); spec["loader"]="pickle"
        with self.assertRaises(ValueError): register_shadow_model(self.db,self.run.run_id,spec,"test")

    def test_backfilled_and_scored_only_after_horizon(self):
        binding=self.binding()
        with patch("app.services.shadow_pipeline.load_closed_history",return_value=self.rows):
            decision=run_shadow(self.db,binding.id,self.clock+timedelta(minutes=10))
            self.assertTrue(decision.backfilled)
            self.assertEqual(score_shadow(self.db,self.clock),0)
        self.rows.append(SimpleNamespace(opened_at=self.clock.replace(minute=0).isoformat(),close=120,volume=1200,content_sha256="future"))
        with patch("app.services.shadow_pipeline.load_closed_history",return_value=self.rows):
            self.assertEqual(score_shadow(self.db,self.clock+timedelta(hours=1)),1)
            self.assertEqual(score_shadow(self.db,self.clock+timedelta(hours=1)),0)
        self.assertFalse(decision.outcome["eligible_for_qualification"])

    def test_future_training_and_blocked_audit(self):
        self.spec["training_cutoff"]="2099-01-01T00:00:00+00:00"
        self.meta["train_label_end"]=self.spec["training_cutoff"]
        self.meta["files"]["logistic_regression.json"]=hashlib.sha256(canonical(self.spec)).hexdigest()
        self.run.manifest_sha256=hashlib.sha256(canonical(self.meta)).hexdigest()
        with self.assertRaises(ValueError): self.binding()

    def test_invalid_data_records_block_without_decision(self):
        binding=self.binding()
        with patch("app.services.shadow_pipeline.load_closed_history",side_effect=ValueError("stale")):
            self.assertIsNone(run_shadow(self.db,binding.id,self.clock))
        self.assertIsNone(self.db.scalar(select(ShadowDecision)))
        self.assertEqual(self.db.scalar(select(ShadowRunAudit)).status,"blocked")

    def test_corrupted_binding_cannot_score(self):
        binding=self.binding()
        with patch("app.services.shadow_pipeline.load_closed_history",return_value=self.rows):
            decision=run_shadow(self.db,binding.id,self.clock)
        self.rows.append(SimpleNamespace(opened_at=self.clock.replace(minute=0).isoformat(),close=120,volume=1200,content_sha256="future"))
        corrupt=copy.deepcopy(binding.spec); corrupt["horizon_bars"]=20
        binding.spec=corrupt
        with patch("app.services.shadow_pipeline.load_closed_history",return_value=self.rows):
            self.assertEqual(score_shadow(self.db,self.clock+timedelta(hours=1)),0)
        self.assertIsNone(decision.outcome)

    def test_conflicting_insert_returns_original(self):
        from app.services.shadow_pipeline import insert_once
        from app.models.shadow import ShadowModelBinding
        original=self.binding()
        duplicate=ShadowModelBinding(run_id=original.run_id,spec_sha256=original.spec_sha256,
            manifest_sha256=original.manifest_sha256,instrument=original.instrument,timeframe_minutes=60,
            spec=original.spec,actor="another")
        self.assertEqual(insert_once(self.db,duplicate,lambda: original).id,original.id)
        self.db.commit()


if __name__ == "__main__":
    unittest.main()
