from concurrent.futures import ThreadPoolExecutor
from datetime import date
import uuid
from threading import Barrier
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.api.stock_learning_cycle as learning_cycle_api
from app.core.config import Settings, settings
from app.core.security import AuthenticationMiddleware, required_role
from app.db.base import Base
from app.models import (
    AuditLog,
    StockDatasetSnapshot,
    StockLearningCycle,
    StockLearningCycleEvent,
    StockModelLifecycleEvent,
    StockModelLifecycleState,
    StockModelRegistry,
    StockPaperBindingState,
    StockPaperModelBinding,
    StockPaperPromotionDecision,
    StockPaperOrder,
    StockPaperTrial,
    StockTrainingJob,
)
from app.services.stock_forward_trial import POLICY
from app.services.stock_learning_cycle import (
    create_learning_cycle,
    cycle_projection,
    review_learning_cycle,
    run_scheduled_paper_trial_handoff_job,
    run_automatic_paper_promotion_job,
    scheduled_learning_control_projection,
    set_scheduled_learning_control,
    start_scheduled_learning_trial,
    sync_cycle_from_training_job,
)


def _db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _postgres_schema_engine():
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("Concurrent scheduled handoff coverage requires PostgreSQL")

    schema = f"task88_{uuid.uuid4().hex}"
    admin_engine = create_engine(settings.database_url, pool_pre_ping=True)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_engine(
        settings.database_url,
        connect_args={"options": f"-csearch_path={schema},public"},
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
    ).execution_options(schema_translate_map={None: schema})
    Base.metadata.create_all(engine)
    return schema, admin_engine, engine


def test_concurrent_scheduled_handoffs_share_one_binding_and_trial():
    schema, admin_engine, engine = _postgres_schema_engine()
    cycle_id = "8" * 64
    snapshot_id = "9" * 64
    model_id = "a" * 64
    job_id = "00000000-0000-0000-0000-000000000088"
    manifest = {
        "purged_expanding_walkforward": True,
        "final_holdout_evaluation_count": 1,
        "final_holdout_consumption": {"count": 1, "job_id": job_id},
        "embargo_days": 0,
    }

    try:
        with Session(engine) as db:
            db.add(StockDatasetSnapshot(
                snapshot_id=snapshot_id, dataset_sha256="b" * 64,
                cutoff_date=date(2026, 9, 12), universe=["SPY"],
                provider="yfinance", feature_config_id="features",
                horizon_days=5, artifact_path="/immutable/snapshot",
                artifact_sha256="c" * 64,
                metadata_json={
                    "binding_eligible": True,
                    "snapshot_id": snapshot_id,
                    "dataset_sha256": "b" * 64,
                    "artifact_path": "/immutable/snapshot",
                    "artifact_sha256": "c" * 64,
                    "provider": "yfinance",
                    "universe": ["SPY"],
                    "horizon_days": 5,
                    "feature_config_id": "features",
                    "identity_sha256": "d" * 64,
                },
            ))
            db.add(StockModelRegistry(
                run_id=model_id, snapshot_id=snapshot_id,
                manifest_sha256="e" * 64, artifact_path="/immutable/model",
                training_metadata=manifest,
            ))
            db.flush()
            db.add(StockModelLifecycleState(
                model_run_id=model_id, lifecycle_state="challenger",
                updated_by="scheduler", reason="scheduled concurrency test",
            ))
            db.add(StockTrainingJob(
                id=job_id, dedupe_key="f" * 64, trigger="scheduled",
                status="succeeded", requested_by="scheduler",
                request_payload={"symbols": ["SPY"], "horizon_bars": 5},
                snapshot_id=snapshot_id, result_run_id=model_id,
            ))
            db.flush()
            db.add(StockLearningCycle(
                cycle_id=cycle_id, request_sha256="1" * 64,
                trigger="scheduled", status="awaiting_admission",
                stage="admission", requested_by="scheduler",
                symbols=["SPY"], cutoff_date=date(2026, 9, 12),
                horizon_days=5, provider="yfinance", seed=42,
                snapshot_id=snapshot_id, training_job_id=job_id,
                model_run_id=model_id, gates={}, evidence={},
                last_reason="awaiting admission",
            ))
            db.commit()

        admission_barrier = Barrier(2)
        starts = []
        from app.services.stock_learning_cycle import (
            sync_cycle_from_training_job as real_sync_cycle_from_training_job,
        )

        def worker():
            with Session(engine) as db:
                return run_scheduled_paper_trial_handoff_job(db)

        def start_trial_once(db, trial_id, *, actor):
            starts.append(trial_id)
            trial = db.get(StockPaperTrial, trial_id)
            trial.status = "running"
            return trial

        def synchronize_handoff(db, job_id, *, actor):
            admission_barrier.wait(timeout=10)
            return real_sync_cycle_from_training_job(db, job_id, actor=actor)

        with (
            patch(
                "app.services.stock_learning_cycle.sync_cycle_from_training_job",
                side_effect=synchronize_handoff,
            ),
            patch(
                "app.services.stock_training_jobs.validate_registered_stock_model",
                return_value=manifest,
            ),
            patch(
                "app.services.stock_training_jobs._dataset_from_record",
                return_value=object(),
            ),
            patch(
                "app.services.stock_training_jobs._validate_holdout_consumption",
            ),
            patch(
                "app.services.stock_forward_trial.validate_registered_stock_model",
                return_value=manifest,
            ),
            patch(
                "app.services.stock_forward_trial._dataset_from_record",
                return_value=object(),
            ),
            patch(
                "app.services.stock_learning_cycle.validate_trial_artifact",
                return_value={},
            ),
            patch(
                "app.services.stock_learning_cycle.trial_feed_preflight",
                return_value={
                    "ready": True,
                    "regular_session": True,
                    "status": "ready",
                },
            ),
            patch(
                "app.services.stock_learning_cycle.start_trial",
                side_effect=start_trial_once,
            ),
        ):
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(lambda _: worker(), range(2)))

        assert {result["status"] for result in results} == {"complete"}
        assert all(
            result["results"][0]["status"] == "running_forward_trial"
            for result in results
        ), [
            (result["results"][0]["status"], result["results"][0]["reason"])
            for result in results
        ]
        assert len(set(result["results"][0]["binding_id"] for result in results)) == 1
        assert len(set(result["results"][0]["trial_id"] for result in results)) == 1
        assert len(starts) == 1

        with Session(engine) as db:
            cycle = db.get(StockLearningCycle, cycle_id)
            assert cycle.binding_id is not None
            assert cycle.trial_id is not None
            assert db.scalar(
                select(StockPaperModelBinding).where(
                    StockPaperModelBinding.source_cycle_id == cycle_id
                )
            ).id == cycle.binding_id
            assert db.scalar(
                select(StockPaperTrial).where(
                    StockPaperTrial.source_cycle_id == cycle_id
                )
            ).id == cycle.trial_id
            assert db.scalar(
                select(StockPaperBindingState.active_binding_id)
            ) == cycle.binding_id
            assert db.query(StockPaperModelBinding).count() == 1
            assert db.query(StockPaperTrial).count() == 1
            assert db.query(StockModelLifecycleEvent).filter_by(
                model_run_id=model_id, to_state="eligible"
            ).count() == 1
            assert db.query(StockModelLifecycleEvent).filter_by(
                model_run_id=model_id, to_state="paper_canary"
            ).count() == 1
            assert db.query(StockLearningCycleEvent).filter_by(
                cycle_id=cycle_id, stage="admission", decision="pass"
            ).count() == 1
            assert db.query(StockPaperOrder).count() == 0
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def test_concurrent_scheduled_handoffs_defer_cross_cycle_paper_binding():
    schema, admin_engine, engine = _postgres_schema_engine()
    cycle_ids = ("a" * 64, "b" * 64)
    snapshot_ids = ("c" * 64, "d" * 64)
    model_ids = ("e" * 64, "f" * 64)
    request_hashes = ("1" * 64, "2" * 64)
    dedupe_keys = ("9" * 64, "8" * 64)
    job_ids = (
        "00000000-0000-0000-0000-000000000099",
        "00000000-0000-0000-0000-000000000100",
    )

    def manifest_for(job_id):
        return {
            "purged_expanding_walkforward": True,
            "final_holdout_evaluation_count": 1,
            "final_holdout_consumption": {"count": 1, "job_id": job_id},
            "embargo_days": 0,
        }

    try:
        with Session(engine) as db:
            for index, (cycle_id, snapshot_id, model_id, job_id) in enumerate(
                zip(cycle_ids, snapshot_ids, model_ids, job_ids)
            ):
                dataset_hash = f"{index + 1}" * 64
                db.add(StockDatasetSnapshot(
                    snapshot_id=snapshot_id, dataset_sha256=dataset_hash,
                    cutoff_date=date(2026, 9, 12), universe=["SPY"],
                    provider="yfinance", feature_config_id="features",
                    horizon_days=5, artifact_path=f"/immutable/snapshot-{index}",
                    artifact_sha256=f"{index + 3}" * 64,
                    metadata_json={
                        "binding_eligible": True,
                        "snapshot_id": snapshot_id,
                        "dataset_sha256": dataset_hash,
                        "artifact_path": f"/immutable/snapshot-{index}",
                        "artifact_sha256": f"{index + 3}" * 64,
                        "provider": "yfinance",
                        "universe": ["SPY"],
                        "horizon_days": 5,
                        "feature_config_id": "features",
                        "identity_sha256": f"{index + 5}" * 64,
                    },
                ))
                db.add(StockModelRegistry(
                    run_id=model_id, snapshot_id=snapshot_id,
                    manifest_sha256=f"{index + 7}" * 64,
                    artifact_path=f"/immutable/model-{index}",
                    training_metadata=manifest_for(job_id),
                ))
                db.flush()
                db.add(StockModelLifecycleState(
                    model_run_id=model_id, lifecycle_state="challenger",
                    updated_by="scheduler", reason="scheduled cross-cycle test",
                ))
                db.add(StockTrainingJob(
                    id=job_id, dedupe_key=dedupe_keys[index],
                    trigger="scheduled", status="succeeded",
                    requested_by="scheduler",
                    request_payload={"symbols": ["SPY"], "horizon_bars": 5},
                    snapshot_id=snapshot_id, result_run_id=model_id,
                ))
                db.flush()
                db.add(StockLearningCycle(
                    cycle_id=cycle_id, request_sha256=request_hashes[index],
                    trigger="scheduled", status="awaiting_admission",
                    stage="admission", requested_by="scheduler",
                    symbols=["SPY"], cutoff_date=date(2026, 9, 12),
                    horizon_days=5, provider="yfinance", seed=42,
                    snapshot_id=snapshot_id, training_job_id=job_id,
                    model_run_id=model_id, gates={}, evidence={},
                    last_reason="awaiting admission",
                ))
            db.commit()

        admission_barrier = Barrier(2)
        started_trial_ids = []

        def worker(cycle_id, job_id):
            with Session(engine) as db:
                sync_cycle_from_training_job(
                    db, job_id, actor="paper_learning_automation"
                )
                admission_barrier.wait(timeout=10)
                cycle = start_scheduled_learning_trial(
                    db, cycle_id, actor="paper_learning_automation"
                )
                db.commit()
                return {
                    "cycle_id": cycle.cycle_id,
                    "status": cycle.status,
                    "binding_id": cycle.binding_id,
                    "trial_id": cycle.trial_id,
                    "reason": cycle.last_reason,
                }

        def start_trial_once(db, trial_id, *, actor):
            started_trial_ids.append(trial_id)
            trial = db.get(StockPaperTrial, trial_id)
            trial.status = "running"
            return trial

        with (
            patch(
                "app.services.stock_training_jobs.validate_registered_stock_model",
                side_effect=lambda model, dataset: model.training_metadata,
            ),
            patch(
                "app.services.stock_training_jobs._dataset_from_record",
                return_value=object(),
            ),
            patch(
                "app.services.stock_training_jobs._validate_holdout_consumption",
            ),
            patch(
                "app.services.stock_forward_trial.validate_registered_stock_model",
                side_effect=lambda model, dataset: model.training_metadata,
            ),
            patch(
                "app.services.stock_forward_trial._dataset_from_record",
                return_value=object(),
            ),
            patch(
                "app.services.stock_learning_cycle.validate_trial_artifact",
                return_value={},
            ),
            patch(
                "app.services.stock_learning_cycle.trial_feed_preflight",
                return_value={
                    "ready": True,
                    "regular_session": True,
                    "status": "ready",
                },
            ),
            patch(
                "app.services.stock_learning_cycle.start_trial",
                side_effect=start_trial_once,
            ),
        ):
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(
                    workers.map(
                        lambda item: worker(*item),
                        zip(cycle_ids, job_ids),
                    )
                )

        assert {result["status"] for result in results} == {
            "running_forward_trial",
            "deferred",
        }
        winner = next(
            result for result in results if result["status"] == "running_forward_trial"
        )
        deferred = next(
            result for result in results if result["status"] == "deferred"
        )
        assert winner["binding_id"] is not None
        assert winner["trial_id"] is not None
        assert deferred["binding_id"] is None
        assert deferred["trial_id"] is None
        assert "retry admission" in deferred["reason"]
        assert len(started_trial_ids) == 1

        with Session(engine) as db:
            cycles = {
                cycle.cycle_id: cycle
                for cycle in db.scalars(
                    select(StockLearningCycle).where(
                        StockLearningCycle.cycle_id.in_(cycle_ids)
                    )
                )
            }
            bindings = db.scalars(select(StockPaperModelBinding)).all()
            trials = db.scalars(select(StockPaperTrial)).all()
            assert len(bindings) == 1
            assert len(trials) == 1
            assert bindings[0].source_cycle_id == winner["cycle_id"]
            assert trials[0].source_cycle_id == winner["cycle_id"]
            assert trials[0].binding_id == bindings[0].id
            assert cycles[winner["cycle_id"]].binding_id == bindings[0].id
            assert cycles[winner["cycle_id"]].trial_id == trials[0].id
            assert cycles[deferred["cycle_id"]].binding_id is None
            assert cycles[deferred["cycle_id"]].trial_id is None
            assert db.scalar(
                select(StockPaperBindingState.active_binding_id)
            ) == bindings[0].id
            winner_model_id = model_ids[cycle_ids.index(winner["cycle_id"])]
            assert db.query(StockModelLifecycleEvent).filter_by(
                model_run_id=winner_model_id,
                to_state="paper_canary",
            ).count() == 1
            assert db.query(StockLearningCycleEvent).filter_by(
                cycle_id=winner["cycle_id"], stage="admission", decision="pass"
            ).count() == 1
            assert db.query(StockLearningCycleEvent).filter_by(
                cycle_id=deferred["cycle_id"], stage="admission", decision="deferred"
            ).count() == 1
            deferred_audits = db.scalars(
                select(AuditLog).where(
                    AuditLog.action == "automatic_paper_trial_handoff",
                    AuditLog.status == "deferred",
                )
            ).all()
            assert len(deferred_audits) == 1
            assert deferred_audits[0].payload["cycle_id"] == deferred["cycle_id"]
            assert deferred_audits[0].payload["binding_id"] is None
            assert deferred_audits[0].payload["trial_id"] is None
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def _client() -> TestClient:
    keys = {
        f"auth_{role}_key": role * 16
        for role in ("viewer", "researcher", "operator", "admin")
    }
    app = FastAPI()
    app.add_middleware(
        AuthenticationMiddleware,
        configuration=Settings(_env_file=None, **keys),
    )
    app.include_router(learning_cycle_api.router)
    app.dependency_overrides[learning_cycle_api.get_db] = lambda: MagicMock()
    return TestClient(app)


def _headers(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {role * 16}"}


def test_learning_cycle_route_roles_cover_list_review_and_action():
    cycle_id = "a" * 64
    assert required_role("GET", "/stock/learning-cycles") == "viewer"
    assert required_role("GET", f"/stock/learning-cycles/{cycle_id}") == "viewer"
    assert required_role("POST", "/stock/learning-cycles") == "researcher"
    assert required_role("POST", f"/stock/learning-cycles/{cycle_id}/review") == "operator"
    assert required_role("POST", f"/stock/learning-cycles/{cycle_id}/action") == "operator"

    with (
        patch.object(learning_cycle_api, "list_learning_cycles", return_value=[]),
        patch.object(learning_cycle_api, "review_learning_cycle", return_value=object()),
        patch.object(learning_cycle_api, "cycle_action", return_value=object()),
        patch.object(learning_cycle_api, "cycle_projection", return_value={"ok": True}),
        patch.object(learning_cycle_api, "_audit"),
    ):
        with _client() as client:
            for role in ("viewer", "researcher", "operator", "admin"):
                with patch.object(
                    learning_cycle_api,
                    "list_learning_cycles",
                    return_value=[],
                ):
                    response = client.get(
                        "/stock/learning-cycles", headers=_headers(role)
                    )
                assert response.status_code == 200, (role, response.text)

                review = client.post(
                    f"/stock/learning-cycles/{cycle_id}/review",
                    headers=_headers(role),
                    json={"reason": "review evidence"},
                )
                action = client.post(
                    f"/stock/learning-cycles/{cycle_id}/action",
                    headers=_headers(role),
                    json={"action": "promote", "reason": "explicit promotion"},
                )
                expected = 200 if role in {"operator", "admin"} else 403
                assert review.status_code == expected, (role, review.text)
                assert action.status_code == expected, (role, action.text)


def test_schedule_control_is_viewer_read_and_operator_write():
    assert required_role("GET", "/stock/learning-cycles/schedule-control") == "viewer"
    assert required_role("POST", "/stock/learning-cycles/schedule-control") == "operator"

    with (
        patch.object(
            learning_cycle_api,
            "scheduled_learning_control_projection",
            return_value={
                "paused": False,
                "pause_reason": None,
                "updated_by": "system",
                "updated_at": None,
                "paper_only": True,
                "live_authorized": False,
                "recovery_independent": True,
            },
        ),
        patch.object(
            learning_cycle_api,
            "set_scheduled_learning_control",
            return_value=object(),
        ),
        patch.object(learning_cycle_api, "write_audit_log"),
    ):
        with _client() as client:
            assert client.get(
                "/stock/learning-cycles/schedule-control",
                headers=_headers("viewer"),
            ).status_code == 200
            for role in ("viewer", "researcher"):
                response = client.post(
                    "/stock/learning-cycles/schedule-control",
                    headers=_headers(role),
                    json={"action": "pause", "reason": "planned maintenance"},
                )
                assert response.status_code == 403, (role, response.text)


def test_paused_scheduled_cycle_is_deferred_without_promotion_or_recovery_change():
    db = _db()
    cycle_id = "9" * 64
    cycle = StockLearningCycle(
        cycle_id=cycle_id,
        request_sha256="8" * 64,
        trigger="scheduled",
        status="operator_review",
        stage="promotion",
        requested_by="scheduler",
        symbols=["SPY"],
        cutoff_date=date(2026, 9, 12),
        horizon_days=5,
        provider="yfinance",
        seed=42,
        training_job_id="00000000-0000-0000-0000-000000000099",
        model_run_id="a" * 64,
        gates={},
        evidence={},
        last_reason="awaiting automatic decision",
    )
    db.add(cycle)
    set_scheduled_learning_control(
        db,
        paused=True,
        actor="operator",
        reason="Investigating model evidence",
    )
    db.commit()

    result = run_automatic_paper_promotion_job(db)
    db.commit()

    assert result["status"] == "paused"
    assert result["evaluated"] == 0
    assert db.query(StockPaperPromotionDecision).filter_by(cycle_id=cycle_id).count() == 0
    db.refresh(cycle)
    assert cycle.status == "deferred"
    assert cycle.last_reason == "Investigating model evidence"
    assert scheduled_learning_control_projection(db)["recovery_independent"] is True
    db.close()


def test_learning_cycle_action_payload_cannot_inject_actor_or_automatic_promotion():
    cycle_id = "b" * 64
    with (
        patch.object(learning_cycle_api, "cycle_action", return_value=object()),
        patch.object(learning_cycle_api, "cycle_projection", return_value={"ok": True}),
        patch.object(learning_cycle_api, "_audit"),
    ):
        with _client() as client:
            for payload in (
                {"action": "promote", "reason": "valid reason", "actor": "admin"},
                {"action": "automatic_promote", "reason": "valid reason"},
                {"action": "promote", "reason": "   "},
            ):
                response = client.post(
                    f"/stock/learning-cycles/{cycle_id}/action",
                    headers=_headers("operator"),
                    json=payload,
                )
                assert response.status_code == 422, (payload, response.text)

            review = client.post(
                f"/stock/learning-cycles/{cycle_id}/review",
                headers=_headers("operator"),
                json={"reason": "  "},
            )
            assert review.status_code == 422


def test_scheduled_cycle_is_visible_as_deferred_and_does_not_claim_coverage():
    db = _db()
    with patch(
        "app.services.stock_learning_cycle.evaluate_cycle_prerequisites",
        return_value={
            "verified_feed": {"status": "unknown", "reason": "regular session required"},
            "scheduler_health": {"status": "pass"},
            "paper_ledger": {"status": "pass"},
            "dataset_provenance": {"status": "pass"},
            "readiness_history": {"status": "pass"},
        },
    ):
        cycle, duplicate = create_learning_cycle(
            db,
            symbols=["SPY"],
            cutoff_at=date(2026, 9, 12),
            horizon_days=5,
            provider="yfinance",
            actor="scheduler",
            trigger="scheduled",
        )
        db.commit()

    assert duplicate is False
    assert cycle.status == "deferred"
    assert cycle.training_job_id is None
    assert cycle.gates["verified_feed"]["status"] == "unknown"
    assert len(db.scalars(select(StockLearningCycleEvent)).all()) == 1

    same, duplicate = create_learning_cycle(
        db,
        symbols=["SPY"],
        cutoff_at=date(2026, 9, 12),
        horizon_days=5,
        provider="yfinance",
        actor="scheduler",
        trigger="scheduled",
    )
    assert duplicate is True
    assert same.cycle_id == cycle.cycle_id
    db.close()


def test_cycle_review_remains_blocked_without_registered_validation_and_forward_evidence():
    db = _db()
    with patch(
        "app.services.stock_learning_cycle.evaluate_cycle_prerequisites",
        return_value={
            "verified_feed": {"status": "fail", "reason": "feed unavailable"},
            "scheduler_health": {"status": "pass"},
            "paper_ledger": {"status": "pass"},
            "dataset_provenance": {"status": "pass"},
            "readiness_history": {"status": "pass"},
        },
    ):
        cycle, _ = create_learning_cycle(
            db,
            symbols=["SPY"],
            cutoff_at=date(2026, 9, 12),
            horizon_days=5,
            provider="yfinance",
            actor="operator",
        )
    reviewed = review_learning_cycle(
        db, cycle.cycle_id, actor="operator", reason="Evidence review requested"
    )
    db.commit()
    projection = cycle_projection(db, reviewed)
    assert reviewed.status == "blocked"
    assert projection["paper_only"] is True
    assert projection["live_authorized"] is False
    assert projection["gates"]["validation"]["status"] == "fail"
    assert projection["gates"]["forward_trial"]["status"] == "unknown"
    assert len(projection["events"]) == 2
    db.close()


def test_scheduled_handoff_retries_transient_preflight_without_blocking_trial():
    db = _db()
    snapshot_id = "c" * 64
    model_id = "d" * 64
    cycle_id = "e" * 64
    db.add(StockDatasetSnapshot(
        snapshot_id=snapshot_id, dataset_sha256="f" * 64, cutoff_date=date(2026, 9, 12),
        universe=["SPY"], provider="yfinance", feature_config_id="features", horizon_days=5,
        artifact_path="/immutable/snapshot", artifact_sha256="a" * 64,
        metadata_json={"binding_eligible": True},
    ))
    db.add(StockModelRegistry(
        run_id=model_id, snapshot_id=snapshot_id, manifest_sha256="b" * 64,
        artifact_path="/immutable/model", training_metadata={},
    ))
    db.add(StockModelLifecycleState(
        model_run_id=model_id, lifecycle_state="paper_canary",
        updated_by="scheduler", reason="scheduled test canary",
    ))
    binding = StockPaperModelBinding(
        model_run_id=model_id, snapshot_id=snapshot_id, binding_sha256="1" * 64,
        purpose="scheduled paper trial", paper_only=True, live_authorized=False,
        bound_by="scheduler", reason="scheduled test canary", source_cycle_id=cycle_id,
    )
    db.add(binding)
    db.flush()
    db.add(StockPaperBindingState(
        id=1, active_binding_id=binding.id, changed_by="scheduler",
        reason="scheduled test canary",
    ))
    trial = StockPaperTrial(
        id="00000000-0000-0000-0000-000000000087", binding_id=binding.id,
        actor="scheduler", source_cycle_id=cycle_id, status="approved",
        policy=dict(POLICY), lineage={"model_run_id": model_id, "snapshot_id": snapshot_id},
    )
    db.add(trial)
    cycle = StockLearningCycle(
        cycle_id=cycle_id, request_sha256="2" * 64, trigger="scheduled",
        status="awaiting_preflight", stage="preflight", requested_by="scheduler",
        symbols=["SPY"], cutoff_date=date(2026, 9, 12), horizon_days=5,
        provider="yfinance", seed=42, snapshot_id=snapshot_id, model_run_id=model_id,
        binding_id=binding.id, trial_id=trial.id, active_binding_id=binding.id,
        gates={}, evidence={}, last_reason="awaiting preflight",
    )
    db.add(cycle)
    db.commit()

    blocked_preflight = {
        "ready": False, "status": "blocked", "regular_session": False,
        "reason": "Regular-session authenticated preflight is required",
    }
    ready_preflight = {
        "ready": True, "status": "ready", "regular_session": True,
        "reason": None, "paper_ledger": {"status": "reconciled"},
    }
    def _start(db, trial_id, actor):
        row = db.get(StockPaperTrial, trial_id)
        row.status = "running"
        return row
    with (
        patch("app.services.stock_learning_cycle.validate_trial_artifact", return_value={}),
        patch("app.services.stock_learning_cycle.trial_feed_preflight", side_effect=[blocked_preflight, ready_preflight]),
        patch("app.services.stock_learning_cycle.start_trial", side_effect=_start),
    ):
        first = start_scheduled_learning_trial(db, cycle_id)
        assert first.status == "deferred"
        assert trial.status == "approved"
        second = start_scheduled_learning_trial(db, cycle_id)
        assert second.status == "running_forward_trial"
        assert trial.status == "running"

    assert cycle.gates["preflight"]["status"] == "pass"
    assert cycle.evidence["preflight"]["ready"] is True
    db.close()