import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

import httpx

from app.services import deployment_monitor

spec = importlib.util.spec_from_file_location("monitor_cli", Path(__file__).parents[1] / "scripts" / "check_deployment_monitor.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class MonitorSecurityTests(unittest.TestCase):
    def test_schedule_definition_returns_explicit_result(self):
        result = deployment_monitor._check_celery_schedule()
        self.assertIn("configured_jobs", result["details"])
        self.assertIn("missing_required_jobs", result["details"])
        self.assertTrue(result["details"]["configured_jobs"])
        self.assertEqual(result["status"], "ready")

    def test_redis_distinguishes_configuration_and_reachability(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(deployment_monitor.settings, "redis_url", ""):
            result = deployment_monitor._check_redis()
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["details"]["configured"])
        self.assertFalse(result["details"]["reachable"])

        with patch.dict("os.environ", {"REDIS_URL": "redis://unreachable"}), patch.object(
            deployment_monitor.settings, "redis_url", "redis://unreachable"
        ), patch.object(
            deployment_monitor.redis.Redis,
            "from_url",
            side_effect=ConnectionError("unreachable"),
        ):
            result = deployment_monitor._check_redis()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["details"]["configured"])
        self.assertFalse(result["details"]["reachable"])

    def test_worker_evidence_requires_a_ping_response(self):
        inspector = MagicMock()
        with patch.object(deployment_monitor.celery_app.control, "inspect", return_value=inspector):
            inspector.active_queues.return_value = {}
            for responses, healthy in (({}, False), ({"worker-a": {"ok": "pong"}}, False), ({"worker-a": {}, "worker-b": {}}, False)):
                with self.subTest(responses=responses):
                    inspector.ping.return_value = responses
                    result = deployment_monitor._check_celery_workers()
                    self.assertEqual(result["status"] == "ready", healthy)
                    self.assertEqual(result["details"]["worker_count"], len(responses))

            inspector.ping.side_effect = RuntimeError("worker unavailable")
            result = deployment_monitor._check_celery_workers()
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["details"]["workers"], [])

    def test_worker_evidence_distinguishes_intraday_and_general_queues(self):
        inspector = MagicMock()
        inspector.ping.return_value = {
            "intraday@host": {"ok": "pong"},
            "general@host": {"ok": "pong"},
        }
        inspector.active_queues.return_value = {
            "intraday@host": [{"name": "intraday_market_data"}],
            "general@host": [{"name": "market_data"}, {"name": "risk"}],
        }
        with patch.object(deployment_monitor.celery_app.control, "inspect", return_value=inspector), patch.object(
            deployment_monitor.celery_app, "send_task"
        ) as send_task:
            result = deployment_monitor._check_celery_workers()

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["details"]["intraday_workers"], ["intraday@host"])
        self.assertEqual(result["details"]["dedicated_intraday_workers"], ["intraday@host"])
        self.assertEqual(result["details"]["general_workers"], ["general@host"])
        self.assertEqual(result["details"]["worker_queues"]["intraday@host"], ["intraday_market_data"])
        self.assertEqual(result["details"]["worker_queues"]["general@host"], ["market_data", "risk"])
        inspector.ping.assert_called_once_with()
        inspector.active_queues.assert_called_once_with()
        send_task.assert_not_called()

    def test_worker_evidence_blocks_when_intraday_queue_is_unserved(self):
        inspector = MagicMock()
        inspector.ping.return_value = {"general@host": {"ok": "pong"}}
        inspector.active_queues.return_value = {"general@host": [{"name": "market_data"}]}
        with patch.object(deployment_monitor.celery_app.control, "inspect", return_value=inspector):
            result = deployment_monitor._check_celery_workers()

        self.assertEqual(result["status"], "blocked")
        self.assertIn("one-minute market polling is blocked", result["message"])
        self.assertEqual(result["details"]["intraday_worker_count"], 0)
        self.assertEqual(result["details"]["dedicated_intraday_worker_count"], 0)
        self.assertEqual(result["details"]["general_workers"], ["general@host"])

    def test_scheduler_evidence_requires_exactly_one_heartbeat(self):
        client = MagicMock()
        with patch.object(deployment_monitor.redis.Redis, "from_url", return_value=client):
            for evidence, healthy in (([], False), (["celery:beat:lease:one"], True), (["celery:beat:lease:one", "celery:beat:lease:two"], False)):
                with self.subTest(evidence=evidence):
                    client.scan_iter.return_value = evidence
                    result = deployment_monitor._check_celery_beat()
                    self.assertEqual(result["status"] == "ready", healthy)
                    self.assertEqual(result["details"]["count"], len(evidence))

    def test_real_viewer_monitor_route_never_creates_risk_settings(self):
        import tempfile
        from alembic import command
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine, select, func
        from sqlalchemy.orm import Session
        from app.api.routes import router
        from app.core.config import Settings
        from app.core.security import AuthenticationMiddleware
        from app.db.session import get_db
        from app.db.schema import migration_config
        from app.models import RiskRule
        with tempfile.TemporaryDirectory() as root:
            engine = create_engine(f"sqlite:///{root}/monitor.db", connect_args={"check_same_thread": False})
            config = migration_config()
            config.set_main_option("sqlalchemy.url", str(engine.url))
            command.upgrade(config, "head")
            app = FastAPI()
            app.add_middleware(
                AuthenticationMiddleware,
                configuration=Settings(
                    _env_file=None,
                    auth_viewer_key="v" * 32,
                    auth_researcher_key="r" * 32,
                    auth_operator_key="o" * 32,
                    auth_admin_key="a" * 32,
                ),
            )
            app.include_router(router)
            def session():
                with Session(engine) as db:
                    yield db
            app.dependency_overrides[get_db] = session
            with TestClient(app) as client, patch.object(deployment_monitor, "_check_redis", return_value={"name": "Redis", "status": "ready", "message": "test", "details": {}}):
                self.assertEqual(client.get("/system/deployment-monitor").status_code, 401)
                response = client.get("/system/deployment-monitor", headers={"Authorization": "Bearer " + "v" * 32})
                self.assertEqual(response.status_code, 200, response.text)
                snapshot = response.json()
                self.assertFalse(snapshot["paper_trading_allowed"])
                risk = next(c for c in snapshot["readiness"]["checks"] if c["name"] == "Risk state")
                self.assertEqual(risk["status"], "blocked")
                self.assertFalse(risk["details"]["risk_rule_present"])
                self.assertEqual(client.post("/system/deployment-monitor/run", headers={"Authorization": "Bearer " + "v" * 32}).status_code, 403)
            with Session(engine) as db:
                self.assertEqual(db.scalar(select(func.count()).select_from(RiskRule)), 0)
            engine.dispose()

    def test_authenticated_nonredirecting_transport(self):
        client = MagicMock()
        client.get.return_value = httpx.Response(200, json={"status": "blocked"}, request=httpx.Request("GET", "https://example.com"))
        with patch.dict("os.environ", {"AUTH_VIEWER_KEY": "v" * 32}), patch.object(monitor.httpx, "Client") as factory:
            factory.return_value.__enter__.return_value = client
            self.assertEqual(monitor._load_snapshot("https://example.com", 5), {"status": "blocked"})
            factory.assert_called_once_with(timeout=5, follow_redirects=False, trust_env=False)
            self.assertEqual(client.get.call_args.kwargs["headers"]["Authorization"], "Bearer " + "v" * 32)
            client.get.return_value = httpx.Response(302, headers={"location": "https://evil.example"}, request=httpx.Request("GET", "https://example.com"))
            with self.assertRaises(httpx.HTTPStatusError):
                monitor._load_snapshot("https://example.com", 5)

    def test_invalid_inputs_fail_before_network(self):
        with patch.dict("os.environ", {"AUTH_VIEWER_KEY": "v" * 32}), patch.object(monitor.httpx, "Client") as factory:
            for url in ("http://example.com", "https://u:secret@example.com", "https://example.com?token=secret", "https://example.com/path"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    monitor._load_snapshot(url, 5)
            with self.assertRaises(ValueError):
                monitor._load_snapshot("https://example.com", float("nan"))
            factory.assert_not_called()

    def test_infrastructure_errors_do_not_expose_secrets(self):
        db = MagicMock()
        db.execute.side_effect = RuntimeError("database-password")
        self.assertNotIn("database-password", str(deployment_monitor._check_database(db)))
        with patch.object(deployment_monitor.redis.Redis, "from_url", side_effect=RuntimeError("redis-password")):
            result = deployment_monitor._check_redis()
        self.assertNotIn("redis-password", str(result))
        self.assertNotIn("redis_url", result["details"])

    def test_persistent_regular_session_breach_reaches_authorized_recovery(self):
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        import app.services.stock_monitoring as stock_monitoring
        from app.db.base import Base
        from app.models import (
            StockDatasetSnapshot,
            StockLearningCycle,
            StockModelLifecycleState,
            StockModelRegistry,
            StockPaperBindingState,
            StockPaperModelBinding,
            StockPaperRecoveryEvent,
            StockPaperRecoveryState,
        )
        from app.models.stock_paper import StockPaperAccount
        from app.services.stock_learning_cycle import cycle_projection, sync_cycle_observability
        from app.services.stock_recovery import (
            rollback_to_last_known_good,
            resume_stock_paper_after_revalidation,
            StockPaperError,
        )

        with TemporaryDirectory() as root:
            engine = create_engine(f"sqlite:///{Path(root) / 'persistent-recovery.db'}")
            Base.metadata.create_all(engine)
            now = datetime(2026, 9, 14, 14, 30, tzinfo=timezone.utc)
            model_run_id = "a" * 64
            snapshot_id = "b" * 64
            with Session(engine) as db:
                db.add(StockDatasetSnapshot(
                    snapshot_id=snapshot_id, dataset_sha256="c" * 64,
                    cutoff_date=now.date(), universe=["SPY"], provider="yfinance",
                    feature_config_id="test", horizon_days=5,
                    artifact_path="/tmp/dataset", artifact_sha256="d" * 64,
                    metadata_json={"test": True},
                ))
                db.add(StockModelRegistry(
                    run_id=model_run_id, snapshot_id=snapshot_id,
                    manifest_sha256="e" * 64, artifact_path="/tmp/model",
                    training_metadata={"selected_model": "test"},
                ))
                db.add(StockModelLifecycleState(
                    model_run_id=model_run_id, lifecycle_state="champion",
                    updated_by="operator", reason="Last-known-good paper model",
                ))
                binding = StockPaperModelBinding(
                    model_run_id=model_run_id, snapshot_id=snapshot_id,
                    binding_sha256="f" * 64, purpose="forward_paper_evaluation",
                    paper_only=True, live_authorized=False, bound_by="operator",
                    reason="Regular-session paper exercise",
                )
                db.add(binding)
                db.flush()
                db.add(StockPaperBindingState(
                    id=1, active_binding_id=binding.id, changed_by="operator",
                    reason="Regular-session paper exercise",
                ))
                db.add(StockLearningCycle(
                    cycle_id="1" * 64, request_sha256="2" * 64, trigger="manual",
                    status="complete", stage="promotion", requested_by="operator",
                    symbols=["SPY"], cutoff_date=now.date(), horizon_days=5,
                    provider="yfinance", seed=42, model_run_id=model_run_id,
                    active_binding_id=binding.id, gates={}, evidence={},
                    last_reason="Paper model was qualified",
                ))
                db.add(StockPaperAccount(
                    broker="alpaca_paper", broker_account_id="paper-account",
                    currency="USD", cash=1000, buying_power=2000, equity=1000,
                    last_equity=1000, status="reconciled", costs_known=True,
                    accounting_verified=True, reconciliation_required=False,
                    unexplained_residual=False, raw_payload={},
                    last_reconciled_at=now,
                ))
                db.commit()

                model_breach = {
                    "key": "model.persistent_breach",
                    "category": "model", "metric": "calibration",
                    "status": "breach", "message": "Model breach",
                    "value": {"degradation": 0.2}, "threshold": {"breach": 0.1},
                    "details": {}, "action_scope": "model",
                }
                safety_breach = {
                    "key": "safety.persistent_breach",
                    "category": "risk", "metric": "daily_loss",
                    "status": "breach", "message": "Safety breach",
                    "value": {"daily_loss": 0.1}, "threshold": {"breach": 0.03},
                    "details": {}, "action_scope": "safety",
                }
                clear_check = {
                    "key": "data.clear_check",
                    "category": "data", "metric": "test_clear",
                    "status": "clear", "message": "Clear",
                    "value": {}, "threshold": {}, "details": {},
                    "action_scope": "none",
                }
                with (
                    patch.object(stock_monitoring, "_now", side_effect=[now, now + timedelta(minutes=1)]),
                    patch.object(stock_monitoring, "_distribution_drift", return_value=[model_breach]),
                    patch.object(stock_monitoring, "_performance_drift", return_value=safety_breach),
                    patch.object(stock_monitoring, "_freshness_and_provenance", return_value=clear_check),
                    patch.object(stock_monitoring, "_execution_divergence", return_value=clear_check),
                    patch.object(stock_monitoring, "_stock_risk_metrics", return_value=[]),
                    patch.object(stock_monitoring, "_broker_reconciliation_health", return_value=[]),
                    patch.object(stock_monitoring, "_worker_scheduler_health", return_value=[]),
                ):
                    first = stock_monitoring.run_stock_monitoring(db, source="regular_session_test")
                    second = stock_monitoring.run_stock_monitoring(db, source="regular_session_test")

                self.assertEqual(first["breaches"][0]["status"], "observed")
                self.assertEqual(second["breaches"][0]["status"], "persistent")
                self.assertEqual(second["status"], "breach")
                self.assertIsNotNone(second["recovery_event_id"])
                self.assertEqual(
                    db.query(StockPaperRecoveryEvent).filter_by(action="pause").count(), 1
                )

                affected = sync_cycle_observability(
                    db,
                    monitor_snapshot_id=second["snapshot_id"],
                    recovery_event_id=second["recovery_event_id"],
                )
                db.commit()
                self.assertEqual(affected, ["1" * 64])
                recovery = db.get(StockPaperRecoveryState, 1)
                self.assertEqual(recovery.status, "cooldown")
                self.assertEqual(recovery.last_known_good_model_run_id, model_run_id)
                self.assertEqual(recovery.last_known_good_binding_id, binding.id)
                self.assertIsNone(db.get(StockPaperBindingState, 1))
                self.assertIsNotNone(db.get(StockPaperModelBinding, binding.id))
                self.assertTrue(binding.paper_only)
                self.assertFalse(binding.live_authorized)

                projection = cycle_projection(db, db.get(StockLearningCycle, "1" * 64))
                self.assertEqual(projection["status"], "demoted")
                self.assertEqual(projection["monitoring"]["snapshot_id"], second["snapshot_id"])
                self.assertEqual(projection["recovery"]["latest_event_id"], second["recovery_event_id"])
                self.assertFalse(projection["live_authorized"])

                snapshot_count = db.query(stock_monitoring.StockMonitoringSnapshot).count()
                recovery_event_count = db.query(StockPaperRecoveryEvent).count()
                stock_monitoring.latest_stock_monitoring(db)
                stock_monitoring.latest_stock_monitoring(db)
                self.assertEqual(db.query(stock_monitoring.StockMonitoringSnapshot).count(), snapshot_count)
                self.assertEqual(db.query(StockPaperRecoveryEvent).count(), recovery_event_count)

                recovery.cooldown_until = datetime.now(timezone.utc) - timedelta(minutes=1)
                account = db.query(StockPaperAccount).one()
                account.status = "reconciled"
                account.reconciliation_required = False
                halted_at = account.halted_at
                if halted_at.tzinfo is None:
                    halted_at = halted_at.replace(tzinfo=timezone.utc)
                clear_at = max(halted_at, now) + timedelta(hours=1)
                db.add(stock_monitoring.StockMonitoringSnapshot(
                    monitor_key=stock_monitoring.MONITOR_KEY, status="clear",
                    generated_at=clear_at, checks=[], actions=[],
                    source="regular_session_revalidation",
                ))
                db.commit()
                with self.assertRaisesRegex(StockPaperError, "explicit actor"):
                    resume_stock_paper_after_revalidation(
                        db, actor="", reason="Revalidate after breach"
                    )
                with patch(
                    "app.services.stock_recovery._now",
                    return_value=clear_at + timedelta(minutes=1),
                ):
                    resumed = resume_stock_paper_after_revalidation(
                        db, actor="operator", reason="Fresh regular-session evidence reviewed"
                    )
                self.assertEqual(resumed["status"], "resumable")
                self.assertEqual(resumed["events"][0]["reason"], "Fresh regular-session evidence reviewed")

                with self.assertRaisesRegex(StockPaperError, "non-empty reason"):
                    rollback_to_last_known_good(db, actor="operator", reason=" ")
                rollback = rollback_to_last_known_good(
                    db, actor="operator", reason="Restore the last-known-good paper binding"
                )
                self.assertEqual(rollback["events"][0]["action"], "rollback")
                self.assertEqual(db.get(StockModelLifecycleState, model_run_id).lifecycle_state, "champion")
                self.assertEqual(db.get(StockPaperBindingState, 1).active_binding_id, binding.id)
            engine.dispose()
