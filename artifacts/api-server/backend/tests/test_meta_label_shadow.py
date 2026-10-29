"""Meta-label shadow mode: records decisions, never changes orders; enforce fails closed."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all tables
from app.core.config import Settings, settings
from app.db.base import Base
from app.models import Strategy, StrategySignal
from app.services import meta_label, meta_label_shadow
from app.services import stock_paper_signal_batch as batch
from app.services.meta_label_shadow import MetaLabelRuntime, resolve_runtime
from app.services.stock_paper_autotrader import run_stock_paper_signal_cycle

from tests.test_meta_label import _tiny_bundle
from tests.test_stock_paper_autotrader import _Db, _account
from tests.test_stock_paper_signal_batch import STRATEGY_STATUSES, _DataDouble

import pytest


def test_mode_setting_is_validated_and_defaults_to_shadow():
    assert Settings().paper_meta_label_mode == "shadow"
    with pytest.raises(ValueError):
        Settings(paper_meta_label_mode="yolo")


def test_missing_artifact_fails_open_in_shadow_and_closed_in_enforce(tmp_path):
    missing = str(tmp_path / "absent.joblib")
    with patch.object(settings, "paper_meta_label_model_path", missing):
        with patch.object(settings, "paper_meta_label_mode", "shadow"):
            runtime = resolve_runtime()
            assert runtime.mode == "off" and not runtime.active and "unavailable" in runtime.reason
        with patch.object(settings, "paper_meta_label_mode", "enforce"):
            runtime = resolve_runtime()
            assert runtime.mode == "enforce" and runtime.bundle is None
            records = runtime.evaluate_symbol("AAPL", None, None, [("macd_momentum", SimpleNamespace(
                action="BUY", confidence=0.6, probability_up=0.6))])
            assert records["macd_momentum"]["decision"] == "skip"
            assert runtime.allows_buy(records["macd_momentum"])[0] is False
            assert runtime.allows_buy(None)[0] is False
        with patch.object(settings, "paper_meta_label_mode", "off"):
            assert resolve_runtime().mode == "off"


def test_shadow_never_vetoes():
    runtime = MetaLabelRuntime(configured_mode="shadow", mode="shadow", path="x", bundle={})
    assert runtime.allows_buy({"decision": "skip"}) == (True, None)
    assert runtime.allows_buy(None) == (True, None)


def test_committed_artifact_loads_and_matches_the_runtime_contract():
    path = meta_label_shadow.model_path()
    if not path.exists():
        pytest.skip("meta-label artifact not present")
    bundle = meta_label.load_model(path)
    assert bundle["format"] == meta_label.MODEL_FORMAT
    assert bundle["feature_names"] == meta_label.feature_names(tuple(bundle["strategies"]))
    assert bundle["policy"]["min_win_rate"] >= 0.8
    with patch.object(settings, "paper_meta_label_mode", "shadow"), \
         patch.object(settings, "paper_meta_label_model_path", ""):
        assert resolve_runtime().mode == "shadow"


# --- autotrader: orders are identical in off and shadow -------------------

def _cycle(mode, meta_record, runtime_bundle=None):
    strategy = SimpleNamespace(id=1, strategy_type="moving_average_crossover")
    db = _Db([strategy])
    order = SimpleNamespace(id=42, status="accepted")
    runtime = MetaLabelRuntime(configured_mode=mode, mode=mode, path="test", bundle=runtime_bundle)

    def fake(_db, symbols, strategies, **kwargs):
        assert kwargs.get("meta_label") is runtime
        signal = {"signal_id": 7, "signal_action": "BUY", "reference_price": "100"}
        if meta_record is not None:
            signal["meta_label"] = meta_record
        return {(symbol, "moving_average_crossover"): dict(signal) for symbol in symbols}, {}

    with patch.object(settings, "paper_execution_symbols", ["AAPL"]), \
         patch("app.services.stock_paper_autotrader.resolve_meta_label_runtime", return_value=runtime), \
         patch("app.services.stock_paper_autotrader.generate_cycle_signals", side_effect=fake), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=_account()), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order", return_value=order) as reserve, \
         patch("app.services.stock_paper_autotrader.dispatch_reserved_order", return_value=order) as dispatch:
        result = run_stock_paper_signal_cycle(db)
    return result, reserve, dispatch


SKIP = {"mode": "shadow", "status": "scored", "probability": 0.41, "threshold": 0.6, "decision": "skip",
        "reason": "calibrated probability 0.410 < threshold 0.600"}


def test_shadow_skip_decision_is_recorded_but_orders_are_unchanged():
    off, off_reserve, off_dispatch = _cycle("off", None)
    shadow, reserve, dispatch = _cycle("shadow", SKIP, runtime_bundle={"threshold": 0.6})
    assert shadow["submitted_count"] == off["submitted_count"] == 1
    # Same order request (the db double is a fresh object per cycle).
    assert [c.kwargs for c in reserve.call_args_list] == [c.kwargs for c in off_reserve.call_args_list]
    assert [c.args[1:] for c in dispatch.call_args_list] == [c.args[1:] for c in off_dispatch.call_args_list]
    row = shadow["results"][0]
    assert row["status"] == "submitted" and row["meta_label"]["decision"] == "skip"
    assert shadow["meta_label"]["mode"] == "shadow"
    assert off["meta_label"]["mode"] == "off"


def test_enforce_skips_rejected_and_unscored_buys_and_allows_take():
    result, reserve, _ = _cycle("enforce", SKIP | {"mode": "enforce"})
    reserve.assert_not_called()
    assert result["results"][0]["status"] == "skipped"
    assert result["results"][0]["reason"].startswith("meta-label enforce")
    _result, reserve, _ = _cycle("enforce", None)
    reserve.assert_not_called()
    _result, reserve, _ = _cycle("enforce", SKIP | {"mode": "enforce", "decision": "take"})
    reserve.assert_called_once()


# --- batched signal pass: same signals, meta record attached to BUYs ----

class ShadowBatchTests(unittest.TestCase):
    def setUp(self):
        batch.clear_signal_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "shadow.sqlite"))
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            for slug, status in STRATEGY_STATUSES.items():
                db.add(Strategy(name=slug, strategy_type=slug, parameters={}, is_active=True, current_status=status))
            db.commit()

    def tearDown(self):
        batch.clear_signal_cache()
        self.engine.dispose()
        self.tmp.cleanup()

    def _run(self, runtime):
        symbols = ["AAA", "BBB", "CCC", "SPY"]
        data = _DataDouble(symbols)
        batch.clear_signal_cache()
        with Session(self.engine) as db:
            stack = data.patches()
            for item in stack:
                item.start()
            try:
                strategies = db.query(Strategy).order_by(Strategy.id).all()
                outcomes, _stats = batch.generate_cycle_signals(db, symbols, strategies, meta_label=runtime)
            finally:
                for item in stack:
                    item.stop()
            features = {(row.symbol, row.strategy_id): row.features for row in db.query(StrategySignal).all()}
        return outcomes, features

    def test_shadow_records_every_buy_without_changing_signals(self):
        plain, _ = self._run(None)
        runtime = MetaLabelRuntime(configured_mode="shadow", mode="shadow", path="test",
                                   bundle=_tiny_bundle(threshold=0.5) | {"sha256": "abc"})
        shadow, features = self._run(runtime)
        self.assertEqual(set(plain), set(shadow))
        buys = 0
        for key, outcome in shadow.items():
            self.assertNotIsInstance(outcome, BaseException)
            self.assertEqual(outcome["signal_action"], plain[key]["signal_action"])
            self.assertEqual(outcome["confidence"], plain[key]["confidence"])
            if outcome["signal_action"] == "BUY":
                buys += 1
                record = outcome["meta_label"]
                self.assertEqual(record["mode"], "shadow")
                if key[1] == "model_predictive_long":
                    self.assertEqual(record["status"], "unsupported_strategy")
                else:
                    self.assertEqual(record["status"], "scored")
                    self.assertIn(record["decision"], {"take", "skip"})
                    self.assertEqual(record["decision"] == "take", record["probability"] >= 0.5)
            else:
                self.assertNotIn("meta_label", outcome)
        self.assertGreater(buys, 0)
        persisted = [value for value in features.values() if "meta_label" in value]
        self.assertEqual(len(persisted), buys)
        self.assertEqual(len(runtime.scored), buys)
        self.assertEqual(runtime.summary()["buy_signals_evaluated"], buys)

    def test_feature_failure_is_recorded_not_raised(self):
        runtime = MetaLabelRuntime(configured_mode="shadow", mode="shadow", path="test",
                                   bundle=_tiny_bundle() | {"sha256": "abc"})
        with patch("app.services.meta_label.latest_market_features", side_effect=RuntimeError("boom")):
            outcomes, _ = self._run(runtime)
        records = [o["meta_label"] for o in outcomes.values() if isinstance(o, dict) and "meta_label" in o]
        self.assertTrue(records)
        self.assertTrue(all(r["status"] == "error" and r["decision"] is None for r in records))
