from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import AgentResearchReport, AgentResearchRun, MarketPrice, ModelPrediction
from app.services.agent_research_reports import (
    create_agent_research_report,
    get_agent_research_report,
    list_agent_research_reports,
)


REPORT_NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


class AgentResearchReportTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            self.engine,
            tables=[
                AgentResearchReport.__table__,
                AgentResearchRun.__table__,
                MarketPrice.__table__,
                ModelPrediction.__table__,
            ],
        )

    def tearDown(self):
        self.engine.dispose()

    def _run(
        self,
        db: Session,
        *,
        symbol: str,
        cutoff: date,
        recommendation: str = "BUY",
        decision_date: date | None = None,
    ) -> AgentResearchRun:
        decision_date = decision_date or cutoff
        row = AgentResearchRun(
            run_id=str(uuid4()),
            dedupe_key=uuid4().hex + uuid4().hex,
            symbol=symbol,
            requested_by="researcher",
            status="completed",
            framework_version="framework-v1",
            prompt_version="prompt-v1",
            model_name="model-v1",
            source_snapshot=[
                {
                    "kind": "daily_price",
                    "source": "frozen-fixture",
                    "observed_at": cutoff.isoformat(),
                    "close": "100",
                }
            ],
            result={"recommendation": recommendation, "rationale": "bounded"},
            evaluation={"status": "pending"},
            usage={"model": "provider-v1"},
            decision_at=datetime.combine(decision_date, datetime.min.time()).replace(
                hour=14, tzinfo=timezone.utc
            ),
        )
        db.add(row)
        db.flush()
        return row

    def _prices(
        self,
        db: Session,
        *,
        symbol: str,
        cutoff: date,
        closes: list[int],
        imported_after: datetime | None = None,
    ) -> None:
        imported_after = imported_after or datetime.combine(
            cutoff, datetime.min.time()
        ).replace(hour=15)
        db.add_all(
            [
                MarketPrice(
                    symbol=symbol,
                    price_date=date.fromordinal(cutoff.toordinal() + offset),
                    close=str(close),
                    volume=1,
                    source="trusted-fixture",
                    imported_at=imported_after.replace(
                        day=min(imported_after.day + offset, 28)
                    ),
                )
                for offset, close in enumerate(closes, start=1)
            ]
        )

    def _prediction(
        self,
        db: Session,
        *,
        symbol: str,
        cutoff: date,
        probability: str = "0.8",
        horizon: int = 5,
        model_version: str | None = "registry-v1",
        created_at: datetime | None = None,
    ) -> ModelPrediction:
        row = ModelPrediction(
            symbol=symbol,
            prediction_date=cutoff,
            horizon_days=horizon,
            probability_up=Decimal(probability),
            probability_down=Decimal("0.2"),
            expected_return=Decimal("0.1"),
            source="existing-model",
            features={"model_version": model_version} if model_version else {},
            probabilities_by_model={"ensemble": float(probability)},
            created_at=created_at or datetime.combine(cutoff, datetime.min.time()).replace(hour=13),
        )
        db.add(row)
        return row

    def test_post_create_refreshes_actual_runs_and_paired_metrics(self):
        with Session(self.engine) as db:
            # BUY is right, HOLD is not directional, and SELL is wrong.  All
            # three are evaluated from persisted prices/predictions, not forged
            # evaluation JSON.
            fixtures = [
                ("AAPL", date(2026, 9, 10), "BUY", [101, 102, 103, 104, 105], "0.8"),
                ("MSFT", date(2026, 9, 10), "HOLD", [99, 98, 97, 96, 95], "0.4"),
                ("QQQ", date(2026, 9, 10), "SELL", [101, 102, 103, 104, 105], "0.2"),
            ]
            rows = []
            for symbol, cutoff, recommendation, closes, probability in fixtures:
                rows.append(self._run(db, symbol=symbol, cutoff=cutoff, recommendation=recommendation))
                self._prices(db, symbol=symbol, cutoff=cutoff, closes=closes)
                self._prediction(
                    db,
                    symbol=symbol,
                    cutoff=cutoff,
                    probability=probability,
                )
            # Four observations leaves this run pending.
            pending = self._run(db, symbol="SPY", cutoff=date(2026, 9, 10))
            self._prices(db, symbol="SPY", cutoff=date(2026, 9, 10), closes=[101, 102, 103, 104])
            db.commit()

            with patch("app.services.agent_research._now", return_value=REPORT_NOW):
                projected = create_agent_research_report(db)

            report = projected["report"]
            self.assertEqual(report["coverage"], {"matched": 3, "pending": 1, "unavailable": 0, "hold": 1})
            self.assertEqual(report["metrics"]["directional_pair_count"], 2)
            self.assertEqual(report["metrics"]["agent_directional_accuracy"], 0.5)
            self.assertEqual(report["metrics"]["baseline_directional_accuracy"], 0.5)
            self.assertEqual(report["metrics"]["baseline_brier_score"], 0.28)
            self.assertEqual(pending.evaluation["status"], "pending")
            self.assertEqual(report["status"], "partial")

            observations = {item["symbol"]: item for item in report["observations"]}
            self.assertEqual(observations["AAPL"]["source_cutoff"], "2026-09-10")
            self.assertEqual(observations["AAPL"]["baseline"]["source"], "existing-model")
            self.assertEqual(observations["AAPL"]["baseline"]["model_version"], "registry-v1")
            self.assertEqual(observations["AAPL"]["provider_model"], "provider-v1")
            self.assertTrue(len(observations["AAPL"]["source_sha256"]) == 64)
            self.assertTrue(observations["AAPL"]["baseline"]["directional_correct"])
            json.dumps(projected)

    def test_valid_baseline_contains_direction_and_missing_model_version_is_null(self):
        with Session(self.engine) as db:
            self._run(db, symbol="AAPL", cutoff=date(2026, 9, 10))
            self._prices(db, symbol="AAPL", cutoff=date(2026, 9, 10), closes=[101, 102, 103, 104, 105])
            self._prediction(
                db,
                symbol="AAPL",
                cutoff=date(2026, 9, 10),
                model_version=None,
            )
            db.commit()
            with patch("app.services.agent_research._now", return_value=REPORT_NOW):
                report = create_agent_research_report(db)["report"]
            baseline = report["observations"][0]["baseline"]
            self.assertEqual(baseline["status"], "complete")
            self.assertTrue(baseline["directional_correct"])
            self.assertIsNone(baseline["model_version"])

    def test_cutoff_previous_day_keeps_fifth_observation_after_decision(self):
        with Session(self.engine) as db:
            cutoff = date(2026, 9, 10)
            row = self._run(
                db,
                symbol="AAPL",
                cutoff=cutoff,
                decision_date=date(2026, 9, 11),
            )
            self._prices(db, symbol="AAPL", cutoff=cutoff, closes=[101, 102, 103, 104, 105])
            self._prediction(db, symbol="AAPL", cutoff=cutoff)
            db.commit()
            with patch("app.services.agent_research._now", return_value=REPORT_NOW):
                report = create_agent_research_report(db)["report"]
            observation = report["observations"][0]
            self.assertEqual(row.evaluation["status"], "complete")
            self.assertEqual(observation["outcome_date"], "2026-09-15")
            self.assertEqual(len(observation["outcome_observations"]), 5)

    def test_wrong_symbol_date_horizon_and_missing_baseline_are_unavailable(self):
        with Session(self.engine) as db:
            cases = [
                ("AAPL", self._prediction, {"symbol": "NOT-AAPL"}),
                ("MSFT", self._prediction, {"symbol": "MSFT", "cutoff": date(2026, 9, 9)}),
                ("QQQ", self._prediction, {"symbol": "QQQ", "horizon": 4}),
                ("SPY", None, {}),
            ]
            rows = []
            for symbol, prediction, kwargs in cases:
                cutoff = date(2026, 9, 10)
                rows.append(self._run(db, symbol=symbol, cutoff=cutoff))
                self._prices(db, symbol=symbol, cutoff=cutoff, closes=[101, 102, 103, 104, 105])
                if prediction is not None:
                    prediction(db, **({"cutoff": cutoff} | kwargs))
            db.commit()
            with patch("app.services.agent_research._now", return_value=REPORT_NOW):
                report = create_agent_research_report(db)["report"]
            self.assertEqual(report["coverage"]["matched"], 0)
            self.assertEqual(report["coverage"]["unavailable"], 4)
            self.assertEqual(report["status"], "unavailable")
            for item in report["observations"]:
                self.assertEqual(item["status"], "unavailable")
                self.assertTrue(item["reason"])

    def test_ambiguous_invalid_and_post_decision_baselines_do_not_match(self):
        with Session(self.engine) as db:
            rows = []
            for symbol in ("AAPL", "MSFT", "QQQ"):
                cutoff = date(2026, 9, 10)
                rows.append(self._run(db, symbol=symbol, cutoff=cutoff))
                self._prices(db, symbol=symbol, cutoff=cutoff, closes=[101, 102, 103, 104, 105])
            self._prediction(db, symbol="AAPL", cutoff=date(2026, 9, 10))
            self._prediction(db, symbol="AAPL", cutoff=date(2026, 9, 10))
            self._prediction(db, symbol="MSFT", cutoff=date(2026, 9, 10), probability="1.2")
            self._prediction(
                db,
                symbol="QQQ",
                cutoff=date(2026, 9, 10),
                created_at=datetime(2026, 9, 10, 15),
            )
            db.commit()
            with patch("app.services.agent_research._now", return_value=REPORT_NOW):
                report = create_agent_research_report(db)["report"]
            self.assertEqual(report["coverage"]["unavailable"], 3)
            reasons = {item["symbol"]: item["reason"] for item in report["observations"]}
            self.assertIn("Multiple", reasons["AAPL"])
            self.assertIn("invalid", reasons["MSFT"])
            self.assertIn("No unambiguous", reasons["QQQ"])

    def test_report_dedup_history_and_frozen_snapshot(self):
        with Session(self.engine) as db:
            cutoff = date(2026, 9, 10)
            self._run(db, symbol="AAPL", cutoff=cutoff)
            self._prices(db, symbol="AAPL", cutoff=cutoff, closes=[101, 102, 103, 104, 105])
            prediction = self._prediction(db, symbol="AAPL", cutoff=cutoff)
            db.commit()
            with patch("app.services.agent_research._now", return_value=REPORT_NOW):
                first = create_agent_research_report(db)
                second = create_agent_research_report(db)
            self.assertEqual(first["report_id"], second["report_id"])
            original_probability = first["report"]["observations"][0]["baseline"]["probability_up"]

            prediction.probability_up = Decimal("0.1")
            db.commit()
            with patch("app.services.agent_research._now", return_value=REPORT_NOW):
                newer = create_agent_research_report(db)
            self.assertNotEqual(first["report_id"], newer["report_id"])
            self.assertEqual(
                get_agent_research_report(db, first["report_id"])["report"]["observations"][0]["baseline"]["probability_up"],
                original_probability,
            )
            self.assertEqual(list_agent_research_reports(db, 10, 0)["total"], 2)

    def test_no_date_objects_escape_report_projection(self):
        with Session(self.engine) as db:
            self._run(db, symbol="AAPL", cutoff=date(2026, 9, 10))
            self._prices(db, symbol="AAPL", cutoff=date(2026, 9, 10), closes=[101, 102, 103, 104, 105])
            self._prediction(db, symbol="AAPL", cutoff=date(2026, 9, 10))
            db.commit()
            with patch("app.services.agent_research._now", return_value=REPORT_NOW):
                projected = create_agent_research_report(db)
            encoded = json.dumps(projected)
            self.assertNotIn("datetime.date", encoded)
            self.assertNotIn("datetime.datetime", encoded)


if __name__ == "__main__":
    unittest.main()