"""Control-plane tests: authorization, durable recovery, and visible failures."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from alembic import command
from alembic.config import Config
import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pandas as pd
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.api.stock_training as stock_training_api
from app.api.stock_training import active_binding, model_report, router
from app.core.config import Settings
from app.core.security import AuthenticationMiddleware, required_role
from app.db.base import Base
from app.core.config import settings
from app.models import (
    Asset, MarketPrice, StockDatasetSnapshot, StockHoldoutReservation,
    StockModelLifecycleEvent, StockModelLifecycleState, StockModelRegistry,
    StockPaperBindingState, StockPaperModelBinding, StockPaperRecoveryState, StockTrainingJob,
)
from app.services.stock_training_jobs import (
    StockTrainingError, create_stock_paper_binding, create_stock_training_job, recover_stock_training_jobs,
    _owned_update, run_stock_training_job, summarize_job, transition_stock_model_lifecycle,
)
from app.services.stock_recovery import rollback_to_last_known_good


def _snapshot() -> StockDatasetSnapshot:
    identity = "a" * 64
    return StockDatasetSnapshot(
        snapshot_id=identity, dataset_sha256="b" * 64, cutoff_date=date(2024, 1, 1),
        universe=["SPY"], provider="yfinance", feature_config_id="c" * 64, horizon_days=5,
        artifact_path="/immutable/snapshot", artifact_sha256="b" * 64,
        metadata_json={
            "snapshot_id": identity, "identity_sha256": identity, "dataset_sha256": "b" * 64,
            "artifact_path": "/immutable/snapshot", "artifact_sha256": "b" * 64,
            "provider": "yfinance", "universe": ["SPY"], "horizon_days": 5,
            "feature_config_id": "c" * 64,
        },
    )


def _job(db: Session, *, status: str = "queued", attempts: int = 0) -> StockTrainingJob:
    snapshot = _snapshot()
    db.add(snapshot)
    db.flush()
    reservation = StockHoldoutReservation(
        universe_key="d" * 64, universe=["SPY"], horizon_days=5,
        period_start=datetime(2023, 1, 1, tzinfo=timezone.utc),
        period_end=datetime(2024, 1, 10, tzinfo=timezone.utc),
        snapshot_id=snapshot.snapshot_id, reserved_by_job_id="12345678-1234-1234-1234-123456789abc",
        purpose="model_selection",
    )
    db.add(reservation)
    db.flush()
    row = StockTrainingJob(
        id="12345678-1234-1234-1234-123456789abc", dedupe_key="e" * 64, trigger="manual",
        status=status, requested_by="researcher", request_payload={"seed": 42, "horizon_bars": 5},
        snapshot_id=snapshot.snapshot_id, holdout_reservation_id=reservation.id, attempts=attempts,
    )
    db.add(row)
    db.commit()
    return row


def test_stock_training_route_roles_are_explicit():
    assert required_role("GET", "/stock/training/jobs") == "viewer"
    assert required_role("GET", "/stock/training/jobs/12345678-1234-1234-1234-123456789abc") == "viewer"
    assert required_role("POST", "/stock/training/jobs") == "researcher"
    assert required_role("POST", "/stock/training/jobs/12345678-1234-1234-1234-123456789abc/cancel") == "researcher"
    assert required_role("POST", "/stock/training/binding") == "operator"
    assert required_role("POST", "/stock/training/jobs/recover") == "admin"
    assert required_role("GET", f"/stock/training/models/{'a' * 64}/lifecycle") == "viewer"
    assert required_role("POST", f"/stock/training/models/{'a' * 64}/lifecycle") == "operator"


def test_stock_training_post_rejects_viewer_before_database_work():
    keys = {f"auth_{role}_key": role * 16 for role in ("viewer", "researcher", "operator", "admin")}
    app = FastAPI()
    app.add_middleware(AuthenticationMiddleware, configuration=Settings(_env_file=None, **keys))
    app.include_router(router)
    client = TestClient(app)
    response = client.post("/stock/training/jobs", headers={"Authorization": "Bearer " + "viewer" * 16}, json={})
    assert response.status_code == 403


def test_lost_running_job_is_redelivered_from_database_state():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        row = _job(db, status="running", attempts=1)
        row.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
        with patch("app.tasks.jobs.stock_training_job.apply_async") as queue:
            queue.return_value.id = "celery-recovery-id"
            recovered = recover_stock_training_jobs(db, stale_after_minutes=1)
        db.commit()
        assert [item.id for item in recovered] == [row.id]
        assert db.get(StockTrainingJob, row.id).status == "queued"
        assert db.get(StockTrainingJob, row.id).celery_task_id == "celery-recovery-id"


def test_recovery_commits_queued_state_before_an_immediate_consumer_runs(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        row = _job(db, status="running", attempts=1)
        row.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
        observed = []

        def consume_immediately(*_args, **_kwargs):
            with Session(engine) as consumer:
                observed.append(consumer.get(StockTrainingJob, row.id).status)
            return type("Delivery", (), {"id": "immediate-consumer"})()

        with patch("app.tasks.jobs.stock_training_job.apply_async", side_effect=consume_immediately):
            recovered = recover_stock_training_jobs(db, stale_after_minutes=1)
        assert [item.id for item in recovered] == [row.id]
        assert observed == ["queued"]


def test_training_failure_is_persisted_and_visible():
    with TemporaryDirectory() as artifacts:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as db, patch.object(settings, "stock_training_artifact_root", artifacts):
            row = _job(db)
            with patch("app.services.stock_training_jobs._dataset_from_record", side_effect=ValueError("immutable snapshot missing")):
                outcome = run_stock_training_job(db, row.id)
            saved = db.get(StockTrainingJob, row.id)
            assert outcome["status"] == "failed"
            assert saved.status == "failed"
            assert saved.failure_code == "ValueError"
            assert "immutable snapshot missing" in saved.failure_detail
            assert not (Path(artifacts) / "models").exists()


def test_cancelled_job_never_publishes_a_model():
    with TemporaryDirectory() as artifacts:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as db, patch.object(settings, "stock_training_artifact_root", artifacts):
            row = _job(db, status="cancel_requested")
            assert run_stock_training_job(db, row.id)["status"] == "cancelled"
            assert db.get(StockTrainingJob, row.id).status == "cancelled"
            assert not (Path(artifacts) / "models").exists()


def test_seeded_provider_rows_run_to_immutable_report_and_paper_binding():
    """Synthetic values are permitted only as rows persisted with real provider provenance."""
    with TemporaryDirectory() as artifacts:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as db, patch.object(settings, "stock_training_artifact_root", artifacts):
            db.add(Asset(symbol="SPY", name="S&P 500", asset_type="stock", is_active=True))
            now = datetime.now(timezone.utc)
            for index, stamp in enumerate(pd.bdate_range(end=date.today(), periods=680)):
                close = 100 + index * .03 + (index % 11 - 5) * .4
                db.add(MarketPrice(
                    symbol="SPY", price_date=stamp.date(), open=close * .998, high=close * 1.01,
                    low=close * .99, close=close, adjusted_close=close, volume=1_000_000 + index,
                    source="yfinance", imported_at=now,
                ))
            db.commit()
            job, duplicate = create_stock_training_job(
                db, symbols=["SPY"], cutoff_at=date.today(), horizon_bars=5,
                provider="yfinance", actor="researcher",
            )
            assert not duplicate
            db.commit()
            outcome = run_stock_training_job(db, job.id)
            assert outcome["status"] == "succeeded", db.get(StockTrainingJob, job.id).failure_detail
            detail = summarize_job(db.get(StockTrainingJob, job.id), db, detail=True)
            assert detail["report"]["dataset_snapshot"]["verified"] is True
            assert detail["report"]["holdout"]["chosen_model"] in {"logistic_regression", "random_forest"}
            assert detail["report"]["holdout"]["chosen_model_metrics"]["log_loss"] is not None
            assert detail["report"]["validation"]["walkforward_comparisons"]
            assert all(item["phase"] in {"purged_walkforward", "calibration", "final_untouched_holdout"} for item in detail["report"]["comparisons"])
            assert detail["report"]["versions"]
            assert detail["report"]["code_sha256"]
            assert detail["report"]["assumptions"]["embargo_days"] == 1
            binding = create_stock_paper_binding(
                db, model_run_id=detail["run_id"], snapshot_id=detail["snapshot_id"],
                actor="operator", purpose="forward_paper_evaluation", reason="seeded integration test",
            )
            db.commit()
            assert binding.paper_only is True and binding.live_authorized is False
            model_dir = Path(artifacts) / "models" / detail["run_id"]
            assert (model_dir / "manifest.json").is_file()
            selected_model = model_dir / "selected_model.joblib"
            original_model = selected_model.read_bytes()
            selected_model.write_bytes(b"tampered")
            with pytest.raises(StockTrainingError, match="integrity"):
                create_stock_paper_binding(
                    db, model_run_id=detail["run_id"], snapshot_id=detail["snapshot_id"],
                    actor="operator", purpose="paper", reason="tampered model must reject",
                )
            invalid = active_binding(db)
            assert invalid["active"] is False and invalid["integrity_valid"] is False
            with pytest.raises(HTTPException) as report_error:
                model_report(detail["run_id"], db)
            assert report_error.value.status_code == 409
            assert "integrity failure" in report_error.value.detail
            selected_model.write_bytes(original_model)
            snapshot_file = Path(db.get(StockDatasetSnapshot, detail["snapshot_id"]).artifact_path) / "dataset.csv"
            snapshot_file.write_bytes(b"tampered")
            with pytest.raises(StockTrainingError, match="snapshot"):
                create_stock_paper_binding(
                    db, model_run_id=detail["run_id"], snapshot_id=detail["snapshot_id"],
                    actor="operator", purpose="paper", reason="tampered snapshot must reject",
                )


def test_schema_has_named_dedupe_and_binding_constraints_and_reusable_dataset_hashes():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    schema = inspect(engine)
    assert "uq_stock_training_job_dedupe" in {item["name"] for item in schema.get_unique_constraints("stock_training_jobs")}
    assert "uq_stock_binding_digest" in {item["name"] for item in schema.get_unique_constraints("stock_paper_model_bindings")}
    snapshot_indexes = {item["name"] for item in schema.get_indexes("stock_dataset_snapshots")}
    assert "ix_stock_dataset_snapshots_dataset_sha256" in snapshot_indexes
    with Session(engine) as db:
        first, second = _snapshot(), _snapshot()
        second.snapshot_id = "f" * 64
        second.metadata_json = second.metadata_json | {"snapshot_id": second.snapshot_id, "identity_sha256": second.snapshot_id}
        db.add_all((first, second))
        db.commit()
        assert db.query(StockDatasetSnapshot).count() == 2


def test_model_lifecycle_is_auditable_without_mutating_immutable_registry_rows():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        model = StockModelRegistry(
            run_id="a" * 64, snapshot_id="b" * 64, manifest_sha256="c" * 64,
            artifact_path="/immutable/model", training_metadata={"selected_model": "test"},
        )
        db.add(model)
        db.flush()
        transition_stock_model_lifecycle(
            db, model_run_id=model.run_id, action="mark_eligible",
            actor="operator", reason="Independent review completed",
        )
        db.commit()
        binding = StockPaperModelBinding(
            model_run_id=model.run_id, snapshot_id=model.snapshot_id,
            binding_sha256="d" * 64, purpose="test", paper_only=True,
            live_authorized=False, bound_by="operator", reason="Canary review",
        )
        db.add(binding)
        db.flush()
        db.add(StockPaperBindingState(
            id=1, active_binding_id=binding.id, changed_by="operator", reason="Canary review",
        ))
        db.commit()
        transition_stock_model_lifecycle(
            db, model_run_id=model.run_id, action="start_canary",
            actor="operator", reason="Start controlled canary",
        )
        db.commit()
        transition_stock_model_lifecycle(
            db, model_run_id=model.run_id, action="promote",
            actor="operator", reason="Explicit promotion decision",
        )
        db.commit()

        assert model.lifecycle_state == "challenger"
        assert db.get(StockModelLifecycleState, model.run_id).lifecycle_state == "champion"
        assert [event.to_state for event in db.scalars(
            select(StockModelLifecycleEvent)
            .where(StockModelLifecycleEvent.model_run_id == model.run_id)
            .order_by(StockModelLifecycleEvent.id)
        ).all()] == ["eligible", "paper_canary", "champion"]


def test_last_known_good_rollback_restores_binding_and_lifecycle_atomically():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        model = StockModelRegistry(
            run_id="e" * 64, snapshot_id="f" * 64, manifest_sha256="1" * 64,
            artifact_path="/immutable/model", training_metadata={"selected_model": "test"},
        )
        db.add(model)
        db.flush()
        transition_stock_model_lifecycle(
            db, model_run_id=model.run_id, action="mark_eligible",
            actor="operator", reason="Eligible for paper canary",
        )
        binding = StockPaperModelBinding(
            model_run_id=model.run_id, snapshot_id=model.snapshot_id,
            binding_sha256="2" * 64, purpose="test", paper_only=True,
            live_authorized=False, bound_by="operator", reason="Canary binding",
        )
        db.add(binding)
        db.flush()
        db.add(StockPaperBindingState(id=1, active_binding_id=binding.id, changed_by="operator", reason="Canary binding"))
        db.commit()
        transition_stock_model_lifecycle(
            db, model_run_id=model.run_id, action="start_canary",
            actor="operator", reason="Start canary",
        )
        db.commit()
        transition_stock_model_lifecycle(
            db, model_run_id=model.run_id, action="promote",
            actor="operator", reason="Promote canary",
        )
        db.commit()
        transition_stock_model_lifecycle(
            db, model_run_id=model.run_id, action="demote",
            actor="monitor", reason="Persistent model breach",
        )
        db.commit()

        recovery = db.get(StockPaperRecoveryState, 1)
        assert recovery.last_known_good_model_run_id == model.run_id
        assert recovery.last_known_good_binding_id == binding.id
        rollback_to_last_known_good(db, actor="operator", reason="Restore last-known-good champion")

        assert db.get(StockModelLifecycleState, model.run_id).lifecycle_state == "champion"
        assert db.get(StockPaperBindingState, 1).active_binding_id == binding.id


def test_stock_migration_head_has_the_same_named_constraints(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'stock-schema.db'}")
    command.upgrade(config, "head")
    migrated = create_engine(config.get_main_option("sqlalchemy.url"))
    schema = inspect(migrated)
    assert "uq_stock_training_job_dedupe" in {item["name"] for item in schema.get_unique_constraints("stock_training_jobs")}
    assert "uq_stock_binding_digest" in {item["name"] for item in schema.get_unique_constraints("stock_paper_model_bindings")}
    assert "ix_stock_dataset_snapshots_dataset_sha256" in {item["name"] for item in schema.get_indexes("stock_dataset_snapshots")}


def test_overlapping_symbol_holdout_is_blocked_even_for_a_different_universe():
    with TemporaryDirectory() as artifacts:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as db, patch.object(settings, "stock_training_artifact_root", artifacts):
            now = datetime.now(timezone.utc)
            for symbol, offset in (("SPY", 0), ("QQQ", 10)):
                db.add(Asset(symbol=symbol, name=symbol, asset_type="stock", is_active=True))
                for index, stamp in enumerate(pd.bdate_range(end=date.today(), periods=120)):
                    close = 100 + offset + index * .1
                    db.add(MarketPrice(
                        symbol=symbol, price_date=stamp.date(), open=close, high=close * 1.01,
                        low=close * .99, close=close, adjusted_close=close, volume=1_000_000,
                        source="yfinance", imported_at=now,
                    ))
            db.commit()
            first, duplicate = create_stock_training_job(
                db, symbols=["SPY"], cutoff_at=date.today(), horizon_bars=5, provider="yfinance", actor="researcher",
            )
            assert not duplicate and first.status == "queued"
            db.commit()
            try:
                create_stock_training_job(
                    db, symbols=["QQQ", "SPY"], cutoff_at=date.today(), horizon_bars=5, provider="yfinance", actor="researcher",
                )
            except StockTrainingError as exc:
                assert "overlaps" in str(exc)
            else:
                raise AssertionError("Intersecting-universe holdout reuse was admitted")
            deferred, duplicate = create_stock_training_job(
                db, symbols=["QQQ", "SPY"], cutoff_at=date.today(), horizon_bars=5,
                provider="yfinance", actor="scheduler", trigger="scheduled",
            )
            assert not duplicate
            assert deferred.status == "deferred"
            assert deferred.holdout_reservation_id is None


def test_fenced_owner_cannot_mutate_a_reclaimed_attempt_and_delivery_cap_is_terminal():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        row = _job(db, status="running", attempts=2)
        row.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.commit()
        # This is the stale worker's generation. A newer worker owns attempt
        # 2, so the old worker cannot heartbeat or terminally mutate it.
        assert not _owned_update(
            db, row.id, 1, ("running",), status="failed",
            failure_code="stale", failure_detail="must not write",
        )
        db.commit()
        current = db.get(StockTrainingJob, row.id)
        assert current.status == "running" and current.attempts == 2
        current.status, current.delivery_attempts = "queued", 3
        current.celery_task_id = None
        db.commit()
        assert recover_stock_training_jobs(db) == []
        assert db.get(StockTrainingJob, row.id).status == "failed"
        assert db.get(StockTrainingJob, row.id).failure_code == "delivery_limit"


def test_duplicate_delivery_cas_only_claims_one_worker_attempt():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        row = _job(db)
        with patch("app.services.stock_training_jobs._dataset_from_record", side_effect=ValueError("worker one failed")):
            first = run_stock_training_job(db, row.id)
            duplicate = run_stock_training_job(db, row.id)
        saved = db.get(StockTrainingJob, row.id)
        assert first["status"] == "failed"
        assert duplicate["status"] == "failed"
        assert saved.attempts == 1


def test_active_admission_bound_rejects_a_fifth_queued_job():
    with TemporaryDirectory() as artifacts:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as db, patch.object(settings, "stock_training_artifact_root", artifacts):
            first = _job(db)
            for suffix in range(3):
                db.add(StockTrainingJob(
                    id=f"00000000-0000-0000-0000-00000000000{suffix}",
                    dedupe_key=f"{suffix + 6:x}" * 64, trigger="manual", status="queued",
                    requested_by="researcher", request_payload={"seed": 42},
                    snapshot_id=first.snapshot_id, holdout_reservation_id=first.holdout_reservation_id,
                ))
            db.add(Asset(symbol="SPY", name="S&P 500", asset_type="stock", is_active=True))
            now = datetime.now(timezone.utc)
            for index, stamp in enumerate(pd.bdate_range(end=date.today(), periods=80)):
                close = 100 + index
                db.add(MarketPrice(
                    symbol="SPY", price_date=stamp.date(), open=close, high=close * 1.01,
                    low=close * .99, close=close, adjusted_close=close, volume=1_000_000,
                    source="yfinance", imported_at=now,
                ))
            db.commit()
            try:
                create_stock_training_job(
                    db, symbols=["SPY"], cutoff_at=date.today(), horizon_bars=5,
                    provider="yfinance", actor="researcher",
                )
            except StockTrainingError as exc:
                assert "admission is full" in str(exc)
            else:
                raise AssertionError("A fifth active job bypassed the admission bound")


def test_historical_snapshot_is_disclosed_research_only_and_cannot_bind():
    with TemporaryDirectory() as artifacts:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as db, patch.object(settings, "stock_training_artifact_root", artifacts):
            db.add(Asset(symbol="SPY", name="S&P 500", asset_type="stock", is_active=True))
            imported = datetime.now(timezone.utc)
            cutoff = date.today() - timedelta(days=2)
            for index, stamp in enumerate(pd.bdate_range(end=cutoff, periods=80)):
                close = 100 + index
                db.add(MarketPrice(
                    symbol="SPY", price_date=stamp.date(), open=close, high=close * 1.01,
                    low=close * .99, close=close, adjusted_close=close, volume=1_000_000,
                    source="yfinance", imported_at=imported,
                ))
            db.commit()
            job, _ = create_stock_training_job(
                db, symbols=["SPY"], cutoff_at=cutoff, horizon_bars=5, provider="yfinance", actor="researcher",
            )
            snapshot = db.get(StockDatasetSnapshot, job.snapshot_id)
            assert snapshot.metadata_json["binding_eligible"] is False
            assert "Historical cutoff" in snapshot.metadata_json["binding_eligibility_reason"]
            db.add(StockModelRegistry(
                run_id="1" * 64, snapshot_id=snapshot.snapshot_id, manifest_sha256="2" * 64,
                artifact_path="/legacy", training_metadata={"legacy": True},
            ))
            db.commit()
            try:
                create_stock_paper_binding(
                    db, model_run_id="1" * 64, snapshot_id=snapshot.snapshot_id,
                    actor="operator", purpose="paper", reason="must reject",
                )
            except StockTrainingError as exc:
                assert "not eligible" in str(exc)
            else:
                raise AssertionError("Historical snapshot was incorrectly bindable")


def test_binding_post_reactivation_appends_a_new_event_and_makes_a_current():
    """A -> B -> A must not return A's historical binding event."""
    with TemporaryDirectory() as artifacts:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        with Session(engine) as db, patch.object(settings, "stock_training_artifact_root", artifacts):
            imported = datetime.now(timezone.utc)
            for symbol, offset in (("SPY", 0), ("QQQ", 15)):
                db.add(Asset(symbol=symbol, name=symbol, asset_type="stock", is_active=True))
                for index, stamp in enumerate(pd.bdate_range(end=date.today(), periods=680)):
                    close = 100 + offset + index * .03 + (index % 11 - 5) * .4
                    db.add(MarketPrice(
                        symbol=symbol, price_date=stamp.date(), open=close * .998, high=close * 1.01,
                        low=close * .99, close=close, adjusted_close=close, volume=1_000_000 + index,
                        source="yfinance", imported_at=imported,
                    ))
            db.commit()
            completed = {}
            for symbol in ("SPY", "QQQ"):
                job, duplicate = create_stock_training_job(
                    db, symbols=[symbol], cutoff_at=date.today(), horizon_bars=5,
                    provider="yfinance", actor="researcher",
                )
                assert not duplicate
                db.commit()
                assert run_stock_training_job(db, job.id)["status"] == "succeeded"
                completed[symbol] = db.get(StockTrainingJob, job.id)

            keys = {f"auth_{role}_key": role * 16 for role in ("viewer", "researcher", "operator", "admin")}
            app = FastAPI()
            app.add_middleware(AuthenticationMiddleware, configuration=Settings(_env_file=None, **keys))
            app.include_router(router)
            app.dependency_overrides[stock_training_api.get_db] = lambda: db
            headers = {"Authorization": "Bearer " + "operator" * 16}
            def activate(symbol: str, reason: str) -> dict:
                job = completed[symbol]
                response = client.post("/stock/training/binding", headers=headers, json={
                    "model_id": job.result_run_id, "snapshot_id": job.snapshot_id,
                    "confirmation": "PAPER_ONLY_FROZEN_BINDING",
                    "purpose": "forward_paper_evaluation", "reason": reason,
                })
                assert response.status_code == 200, response.text
                return response.json()

            with TestClient(app) as client:
                first_a = activate("SPY", "activate A for paper evaluation")
                binding_b = activate("QQQ", "activate B for paper evaluation")
                second_a = activate("SPY", "reactivate A for paper evaluation")
                current = client.get(
                    "/stock/training/binding",
                    headers={"Authorization": "Bearer " + "viewer" * 16},
                )
            assert current.status_code == 200
            assert first_a["model_id"] == completed["SPY"].result_run_id
            assert binding_b["model_id"] == completed["QQQ"].result_run_id
            assert second_a["model_id"] == completed["SPY"].result_run_id
            assert first_a["binding_id"] != second_a["binding_id"]
            assert first_a["binding_sha256"] != second_a["binding_sha256"]
            assert current.json()["model_id"] == completed["SPY"].result_run_id
            rows = db.scalars(select(StockPaperModelBinding).order_by(StockPaperModelBinding.id)).all()
            assert len(rows) == 3
            assert [row.model_run_id for row in rows] == [
                completed["SPY"].result_run_id,
                completed["QQQ"].result_run_id,
                completed["SPY"].result_run_id,
            ]
