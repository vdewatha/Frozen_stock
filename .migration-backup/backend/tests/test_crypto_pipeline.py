from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from app.db.base import Base
from app.integrations.kraken import BTC_USD
from app.models.crypto_data import CryptoCandle, CollectionRun
from app.models.shadow import ShadowDecision, ShadowModelBinding
from app.services.crypto_pipeline import run_crypto_cycle
from app.services.crypto_collection import load_closed_history, CryptoDataError
from app.services.instruments import Candle, Timeframe
from app.services.research_training import train_research_run
from app.services.research_registry import register_research_run
from app.services.shadow_pipeline import register_shadow_model, canonical


class CryptoPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.engine = create_engine("sqlite:///"+self.temp.name+"/pipeline.db")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.clock = datetime(2025, 1, 10, 12, 1, tzinfo=timezone.utc)
        x = np.arange(400)
        prices = pd.DataFrame({"date": pd.date_range("2020-01-01", periods=400, freq="h", tz="UTC"),
                              "close": 100+np.sin(x)*4+x/100, "volume": x+1000})
        manifest = train_research_run(prices, Path(self.temp.name)/"research", symbol="BTC/USD", source="fixture",
            instrument_id=BTC_USD.instrument_id, timeframe_minutes=60)
        folder = (Path(self.temp.name)/"research"/manifest["run_id"]).resolve()
        spec = json.loads((folder/"logistic_regression.json").read_text())
        with self.sessions.begin() as db:
            run = register_research_run(db, folder)
            self.binding_id = register_shadow_model(db, run.run_id, spec, "fixture-reviewer").id
        self.client = Mock()
        self.client.ohlc.return_value = [Candle(BTC_USD, Timeframe.HOUR,
            self.clock.replace(minute=0)-timedelta(hours=120-i), 100, 106, 95, 100+np.sin(i)*4, 1000+i) for i in range(120)]

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def test_actual_collection_model_shadow_integrity_and_duplicate(self):
        with patch("joblib.load", side_effect=AssertionError("No executable artifacts")):
            first = run_crypto_cycle(self.sessions, client=self.client, as_of=self.clock)
            again = run_crypto_cycle(self.sessions, client=self.client, as_of=self.clock)
        self.assertEqual(first["status"], "success")
        self.assertEqual(first["inserted_count"], 120)
        self.assertEqual(again["inserted_count"], 0)
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ShadowDecision)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(CollectionRun)), 2)
            decision = db.scalar(select(ShadowDecision))
            rows = load_closed_history(db, as_of=self.clock)
            self.assertEqual(decision.data_sha256, hashlib.sha256(canonical([r.content_sha256 for r in rows])).hexdigest())
            self.assertFalse(decision.backfilled)
            self.assertFalse(decision.eligible_for_qualification)
            with self.assertRaises(CryptoDataError):
                load_closed_history(db, as_of=self.clock-timedelta(seconds=30))

    def test_blocked_collection_commits_audit_without_shadow(self):
        self.client.ohlc.return_value = self.client.ohlc.return_value[:-1]
        result = run_crypto_cycle(self.sessions, client=self.client, as_of=self.clock)
        self.assertEqual(result["status"], "blocked")
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ShadowDecision)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(CryptoCandle)), 0)
            self.assertEqual(db.scalar(select(CollectionRun)).error_code, "stale_history")

    def test_invalid_binding_isolated_from_valid_binding_and_collection(self):
        with self.sessions.begin() as db:
            good = db.get(ShadowModelBinding, self.binding_id)
            bad = ShadowModelBinding(run_id=good.run_id, spec_sha256="b"*64,
                manifest_sha256=good.manifest_sha256, instrument=good.instrument,
                timeframe_minutes=60, spec=dict(good.spec, intercept=99), actor="corruption-fixture")
            db.add(bad)
            db.flush()
            bad_id = bad.id
        result = run_crypto_cycle(self.sessions, client=self.client, as_of=self.clock)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["blocked_bindings"], [bad_id])
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(CryptoCandle)), 120)
            self.assertEqual(db.scalar(select(func.count()).select_from(ShadowDecision)), 1)

    def test_scoring_failure_does_not_rollback_collection_or_shadow(self):
        with patch("app.services.crypto_pipeline.score_shadow", side_effect=RuntimeError("test")):
            with self.assertRaises(RuntimeError):
                run_crypto_cycle(self.sessions, client=self.client, as_of=self.clock)
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(CryptoCandle)), 120)
            self.assertEqual(db.scalar(select(func.count()).select_from(ShadowDecision)), 1)
        result = run_crypto_cycle(self.sessions, client=self.client, as_of=self.clock)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["inserted_count"], 0)
