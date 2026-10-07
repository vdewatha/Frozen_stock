from __future__ import annotations

from datetime import date, datetime, timedelta
from hashlib import sha256
from decimal import Decimal
import logging
import time
from typing import Callable

import redis
from sqlalchemy import text
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import DBAPIError, InterfaceError, InternalError, OperationalError
from sqlalchemy.types import JSON

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.economic_data import import_fallback_economic_indicators
from app.services.decision_journal import refresh_decision_journal_outcomes, update_strategy_memory_from_journal
from app.services.deployment_monitor import run_deployment_monitor
from app.services.experiments import run_strategy_experiments
from app.services.governance import evaluate_strategy_governance
from app.services.market_data import import_market_prices
from app.services.intraday_data import ingest_corporate_actions, collect_scheduled_intraday
from app.services.market_regime import detect_and_store_market_regime
from app.services.memory_replay import run_memory_replay_gate_monitor
from app.services.model_tracking import run_and_persist_model_predictions, score_realized_predictions
from app.services.news_sentiment import import_mock_news
from app.services.notifications import create_notification, resolve_successful_job_notifications
from app.services.paper_trading import reconcile_open_paper_trades, run_paper_signal, update_all_strategy_memory
from app.services.risk_actions import evaluate_portfolio_risk_actions
from app.services.trade_candidates import get_trade_candidate_snapshot
from app.services.stock_training_jobs import (
    StockTrainingError,
    create_stock_training_job,
    enqueue_stock_training_job,
    recover_stock_training_jobs,
    run_stock_training_job,
)
from app.services.stock_monitoring import run_stock_monitoring
from app.services.stock_recovery import reconcile_inflight_stock_orders_on_restart
from app.services.live_broker import LiveBrokerError, reconcile_live_broker_account
from app.services.stock_forward_trial import observe_trial, execute_pending_decisions, start_trial, evaluate_trial
from app.models import StockPaperTrial
from app.tasks.celery_app import celery_app
from app.models import Asset, Notification, PaperTrade, Strategy, StockTrainingJob
from app.services.stock_learning_cycle import (
    create_learning_cycle,
    run_automatic_paper_promotion_job,
    run_scheduled_paper_trial_handoff_job,
    scheduled_learning_control_projection,
    sync_cycle_from_training_job,
    sync_cycle_from_trial,
    sync_cycle_observability,
)
from app.services.agent_research import run_agent_research, refresh_agent_research_evaluations

logger = logging.getLogger(__name__)


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _normalize_pending_json(db) -> None:
    """Prevent provider/lineage JSON columns from receiving Decimal values."""
    # Include loaded persistent objects because SQLAlchemy's mutable JSON
    # tracking does not always mark a nested dictionary dirty.
    for instance in (*db.new, *db.dirty, *db.identity_map.values()):
        for attribute in sqlalchemy_inspect(instance).mapper.column_attrs:
            column = attribute.columns[0]
            if isinstance(column.type, JSON):
                value = getattr(instance, attribute.key)
                setattr(instance, attribute.key, _json_safe(value))


REDIS_LOCKED_JOBS = frozenset(
    {
        "intraday_market_data_import",
        "iex_research_collection_job",
        "delayed_sip_collection_job",
        "stock_forward_trial_observe_job",
        "stock_forward_trial_reconcile_job",
        "stock_monitoring_job",
    }
)
REDIS_JOB_LOCK_TTL_SECONDS = 15 * 60
INTRADAY_TASK_SOFT_TIME_LIMIT_SECONDS = 45
INTRADAY_TASK_TIME_LIMIT_SECONDS = 55
INTRADAY_JOB_LOCK_TTL_SECONDS = INTRADAY_TASK_TIME_LIMIT_SECONDS + 65
IEX_TASK_SOFT_TIME_LIMIT_SECONDS = 110
IEX_TASK_TIME_LIMIT_SECONDS = 120
LEARNING_TASK_SOFT_TIME_LIMIT_SECONDS = 12 * 60
LEARNING_TASK_TIME_LIMIT_SECONDS = 15 * 60
IEX_JOB_LOCK_TTL_SECONDS = IEX_TASK_TIME_LIMIT_SECONDS + 60
REDIS_LOCK_RETRY_DELAYS_SECONDS = (0.25, 0.75, 1.5)
FEATURE_REFRESH_THROTTLE_SECONDS = 10 * 60
FEATURE_REFRESH_THROTTLE_KEY = "trading:scheduled-refresh:daily_feature_generation"


def _queue_feature_refresh() -> str | None:
    """Queue at most one IEX-triggered feature refresh per throttle window."""
    client = redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=15,
    )
    try:
        if not client.set(
            FEATURE_REFRESH_THROTTLE_KEY,
            "1",
            nx=True,
            ex=FEATURE_REFRESH_THROTTLE_SECONDS,
        ):
            return None
        try:
            return daily_feature_generation.apply_async(
                queue="market_data", expires=60 * 60
            ).id
        except Exception:
            client.delete(FEATURE_REFRESH_THROTTLE_KEY)
            raise
    finally:
        client.close()


def _close_job_lock_client(client, job_name: str) -> None:
    try:
        client.close()
    except Exception as exc:
        # Cleanup must not hide the work result or the original failure.
        logger.error(
            "Scheduled job Redis client cleanup failed: job=%s error_type=%s",
            job_name, type(exc).__name__,
        )


def _acquire_job_lock(job_name: str):
    """Acquire a Redis lease for jobs whose cadence must not overlap.

    PostgreSQL advisory locks additionally protect PostgreSQL deployments.
    Redis provides coordination for both PostgreSQL and SQLite. An outage is
    an operational failure, not permission to
    run an uncoordinated market-data or broker-reconciliation task.
    """
    client = redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=15,
    )
    acquired = False
    try:
        last_error = None
        for delay in (0.0, *REDIS_LOCK_RETRY_DELAYS_SECONDS):
            if delay:
                time.sleep(delay)
            try:
                client.ping()
                last_error = None
                break
            except redis.RedisError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        lock_ttl = (
            INTRADAY_JOB_LOCK_TTL_SECONDS
            if job_name == "intraday_market_data_import"
            else IEX_JOB_LOCK_TTL_SECONDS
            if job_name == "iex_research_collection_job"
            else REDIS_JOB_LOCK_TTL_SECONDS
        )
        lock = client.lock(
            f"trading:scheduled-job:{job_name}",
            timeout=lock_ttl,
            blocking=False,
        )
        if not lock.acquire(blocking=False):
            return None
        acquired = True
        return lock
    except redis.RedisError as exc:
        raise RuntimeError(
            f"Redis coordination is unavailable for scheduled job {job_name}"
        ) from exc
    finally:
        if not acquired:
            _close_job_lock_client(client, job_name)


def _run_job(job_name: str, work: Callable) -> dict:
    db = SessionLocal()
    lock_key = int.from_bytes(sha256(f"job:{job_name}".encode()).digest()[:8], "big") % 2_147_483_647
    lock_acquired = False
    lock_acquisition_confirmed = False
    lock_connection = None
    redis_lock = None
    try:
        if job_name in REDIS_LOCKED_JOBS:
            redis_lock = _acquire_job_lock(job_name)
            if redis_lock is None:
                return {
                    "status": "skipped",
                    "job": job_name,
                    "reason": "duplicate scheduled job lease is active",
                }
        if db.get_bind().dialect.name == "postgresql":
            # Session.commit() returns its connection to the pool. Keep the
            # session-level advisory lock on a separately owned connection.
            lock_connection = db.get_bind().connect()
            lock_acquired = bool(lock_connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}))
            lock_acquisition_confirmed = True
            if not lock_acquired:
                return {"status": "skipped", "job": job_name, "reason": "duplicate worker lease is active"}
        result = work(db)
        # A retry that completes successfully is evidence that the prior
        # incident is no longer active. Keep unrelated operator alerts open.
        if isinstance(result, dict) and result.get("status") in {
            "complete",
            "observed",
            "ready",
            "success",
            # A scheduled reconciliation with no initialized paper account
            # completed its safety preflight and is a successful no-op.
            "uninitialized",
            # Stock paper recovery returns this after importing broker truth
            # and reconciling the durable ledger.
            "reconciled",
            # Monitoring can complete successfully while reporting a safety
            # breach. The breach remains in the monitoring snapshot; this
            # status only means the scheduled job itself did not fail.
            "breach",
            # Deployment monitoring can complete and persist a truthful
            # blocked readiness snapshot without being a failed job.
            "blocked",
        }:
            try:
                if resolve_successful_job_notifications(db, job_name):
                    db.commit()
            except Exception as notification_error:
                # Notification cleanup is auxiliary. A successful market-data
                # or reconciliation job must not be converted into a failure
                # merely because an older/minimal database lacks notification
                # tables or the notification store is temporarily unavailable.
                logger.error(
                    "Successful scheduled job notification cleanup unavailable: job=%s error_type=%s",
                    job_name, type(notification_error).__name__,
                )
                db.rollback()
        return result
    except Exception as exc:
        # Failure reporting must never commit pending effects of failed work.
        try:
            db.rollback()
            create_notification(
                db,
                category="scheduled_job",
                severity="critical",
                source=job_name,
                title=f"Scheduled job failed: {job_name}",
                message=str(exc),
                entity_type="job",
                payload={"job": job_name, "error": str(exc), "error_type": exc.__class__.__name__},
            )
            db.commit()
        except Exception as notification_error:
            # Row values and provider errors may contain secrets; log types only.
            logger.error(
                "Scheduled job failure notification unavailable: job=%s error_type=%s notification_error_type=%s",
                job_name, type(exc).__name__, type(notification_error).__name__,
            )
            db.rollback()
        raise
    finally:
        if lock_connection is not None:
            try:
                if not lock_acquisition_confirmed:
                    raise RuntimeError("Scheduled job advisory lock acquisition is uncertain")
                if lock_acquired and not lock_connection.scalar(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
                ):
                    raise RuntimeError("Scheduled job advisory lock ownership was lost")
            except Exception:
                logger.error("Scheduled job lock cleanup failed: job=%s", job_name)
                # A rollback does not release session-level locks. Never return
                # a connection with uncertain lock ownership to the pool.
                lock_connection.invalidate()
            finally:
                lock_connection.close()
        if redis_lock is not None:
            try:
                redis_lock.release()
            except Exception as exc:
                # Expiry/lost ownership can indicate that work outlived its
                # lease. Surface it without retrying already-completed work or
                # deleting a successor's token. Redis release checks ownership.
                logger.error(
                    "Scheduled job Redis lease cleanup failed: job=%s error_type=%s",
                    job_name, type(exc).__name__,
                )
            finally:
                _close_job_lock_client(redis_lock.redis, job_name)
        db.close()


@celery_app.task
def daily_market_data_import() -> dict:
    def work(db):
        assets = db.query(Asset).filter(Asset.is_active.is_(True)).order_by(Asset.symbol).all()
        results = [import_market_prices(db, asset.symbol, "2y") for asset in assets]
        corporate_actions = ingest_corporate_actions(db, [asset.symbol for asset in assets])
        journal = refresh_decision_journal_outcomes(db, source="daily_market_data_import", notify=True)
        replay_monitor = run_memory_replay_gate_monitor(db, source="daily_market_data_import", limit=60, top_k=3)
        agent_evaluation_updates = refresh_agent_research_evaluations(db)
        trusted_imported = sum(1 for result in results if result.get("trusted") is True)
        learning_refresh = None
        if trusted_imported:
            # Queue learning only after the new price history is committed by
            # this job. The batch remains paper-only and promotion-gated.
            learning_refresh = strategy_learning_batch_job.apply_async(
                queue="learning", expires=60 * 60
            ).id
        return {
            "status": "complete",
            "job": "daily_market_data_import",
            "results": results,
            "trusted_assets": trusted_imported,
            "learning_refresh_task_id": learning_refresh,
            "corporate_actions": corporate_actions,
            "decision_journal": journal,
            "memory_replay_gate_monitor": replay_monitor,
            "agent_research_evaluation_updates": agent_evaluation_updates,
        }

    return _run_job("daily_market_data_import", work)

@celery_app.task(
    soft_time_limit=INTRADAY_TASK_SOFT_TIME_LIMIT_SECONDS,
    time_limit=INTRADAY_TASK_TIME_LIMIT_SECONDS,
    acks_late=True,
    reject_on_worker_lost=True,
)
def intraday_market_data_import() -> dict:
    def work(db):
        result = collect_scheduled_intraday(db)
        db.commit()
        return {"job": "intraday_market_data_import", **result}

    return _run_job("intraday_market_data_import", work)


@celery_app.task
def daily_news_import() -> dict:
    def work(db):
        assets = db.query(Asset).filter(Asset.is_active.is_(True)).order_by(Asset.symbol).all()
        results = [import_mock_news(db, asset.symbol) for asset in assets]
        return {"status": "complete", "job": "daily_news_import", "results": results}

    return _run_job("daily_news_import", work)


@celery_app.task(soft_time_limit=IEX_TASK_SOFT_TIME_LIMIT_SECONDS, time_limit=IEX_TASK_TIME_LIMIT_SECONDS)
def iex_research_collection_job() -> dict:
    if not settings.iex_research_enabled:
        return {"status": "disabled", "research_only": True}

    def work(db):
        from app.services.alpaca_research_data import collect_iex_research
        result = collect_iex_research(db)
        db.commit()
        from app.services.online_research import advance_online_research
        result["learning"] = advance_online_research(db)
        db.commit()
        if result.get("status") == "observed" and not result.get("synthetic"):
            # IEX is research-only, but a successful real collection is a
            # trustworthy trigger for fresh multi-symbol predictions. The
            # feature task remains paper-only and never dispatches orders. A
            # bounded throttle prevents one full feature run from being
            # enqueued for every one-minute observation.
            refresh_id = _queue_feature_refresh()
            if refresh_id:
                result["feature_refresh_task_id"] = refresh_id
            else:
                result["feature_refresh_throttled"] = True
        return result

    return _run_job("iex_research_collection_job", work)


@celery_app.task(soft_time_limit=110, time_limit=120)
def delayed_sip_collection_job() -> dict:
    if not settings.delayed_sip_research_enabled:
        return {"status": "disabled", "research_only": True}

    def work(db):
        from app.services.delayed_sip_research import collect_delayed_sip
        result = collect_delayed_sip(db)
        db.commit()
        return result

    return _run_job("delayed_sip_collection_job", work)


@celery_app.task
def daily_feature_generation() -> dict:
    def work(db):
        # Materialize scalar symbols before releasing the read transaction.
        # Keeping expired ORM objects here makes the first later ``asset.symbol``
        # access issue a database reload after model work has already started.
        symbols = [
            symbol
            for (symbol,) in db.query(Asset.symbol)
            .filter(Asset.is_active.is_(True))
            .order_by(Asset.symbol)
            .all()
        ]
        # Do not keep the asset-query transaction open while each symbol trains
        # models and refreshes its candidate snapshot.
        db.commit()
        results = [run_and_persist_model_predictions(db, symbol) for symbol in symbols]
        candidate_snapshot = get_trade_candidate_snapshot(db, limit=12, refresh=True)
        return {
            "status": "complete",
            "job": "daily_feature_generation",
            "model_runs": [{"symbol": result["symbol"], "saved_prediction_ids": result["saved_prediction_ids"]} for result in results],
            "candidate_scan": {
                "candidate_count": candidate_snapshot["candidate_count"],
                "positive_count": candidate_snapshot["positive_count"],
                "cache_status": candidate_snapshot["cache_status"],
            },
        }

    return _run_job("daily_feature_generation", work)


@celery_app.task
def model_realization_scoring_job() -> dict:
    """Score matured trusted predictions for the research feedback loop.

    This records out-of-sample prediction outcomes only. It does not create
    trades, alter paper bindings, or promote a model.
    """
    def work(db):
        result = score_realized_predictions(db)
        return {
            "status": "complete",
            "job": "model_realization_scoring_job",
            "paper_only": True,
            "live_authorized": False,
            **result,
        }

    return _run_job("model_realization_scoring_job", work)


@celery_app.task
def daily_economic_data_import() -> dict:
    def work(db):
        result = import_fallback_economic_indicators(db)
        return {"status": "complete", "job": "daily_economic_data_import", **result}

    return _run_job("daily_economic_data_import", work)


@celery_app.task
def daily_market_regime_detection() -> dict:
    def work(db):
        result = detect_and_store_market_regime(db, "SPY")
        return {"status": "complete", "job": "daily_market_regime_detection", "regime": result}

    return _run_job("daily_market_regime_detection", work)


@celery_app.task
def nightly_backtest_job() -> dict:
    def work(db):
        strategy = db.query(Strategy).filter(Strategy.strategy_type == "moving_average_crossover").one_or_none()
        assets = db.query(Asset).filter(Asset.is_active.is_(True)).order_by(Asset.symbol).all()
        if not strategy:
            return {"status": "skipped", "job": "nightly_backtest_job", "reason": "No moving average strategy configured."}
        results = [
            run_strategy_experiments(db, symbol=asset.symbol, strategy_slug=strategy.strategy_type, max_candidates=3, apply_promotions=False)
            for asset in assets
        ]
        return {
            "status": "complete",
            "job": "nightly_backtest_job",
            "experiments": [{"symbol": result["symbol"], "created": len(result["experiments"])} for result in results],
        }

    return _run_job("nightly_backtest_job", work)


@celery_app.task(
    autoretry_for=(DBAPIError, InterfaceError, InternalError, OperationalError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=2,
    soft_time_limit=LEARNING_TASK_SOFT_TIME_LIMIT_SECONDS,
    time_limit=LEARNING_TASK_TIME_LIMIT_SECONDS,
    acks_late=True,
    reject_on_worker_lost=True,
)
def strategy_learning_scope_job(symbol: str, strategy_slug: str, max_candidates: int = 3) -> dict:
    def work(db):
        # Keep each scheduled scope bounded, while honoring the caller's
        # candidate budget so recurring fan-out explores more than the first
        # proposal when worker capacity allows it.
        scoped_candidates = max(1, min(int(max_candidates), 3))
        result = run_strategy_experiments(
            db,
            symbol=symbol,
            strategy_slug=strategy_slug,
            max_candidates=scoped_candidates,
            apply_promotions=False,
        )
        return {
            "status": "complete",
            "job": "strategy_learning_scope_job",
            "symbol": result["symbol"],
            "strategy": result["strategy"],
            "source": result["source"],
            "baseline_score": result["baseline_score"],
            "experiments": [
                {
                    "id": experiment.id,
                    "name": experiment.experiment_name,
                    "decision": experiment.decision,
                }
                for experiment in result["experiments"]
            ],
            "applied_parameters": result["applied_parameters"],
            "paper_only": True,
        }

    return _run_job(f"strategy_learning_scope_job:{symbol}:{strategy_slug}", work)


@celery_app.task
def strategy_learning_batch_job(limit_symbols: int = 25, limit_strategies: int = 25, max_candidates: int = 3) -> dict:
    def work(db):
        assets = db.query(Asset).filter(Asset.is_active.is_(True)).order_by(Asset.symbol).limit(max(1, min(limit_symbols, 25))).all()
        strategies = db.query(Strategy).order_by(Strategy.name).limit(max(1, min(limit_strategies, 25))).all()
        queued = []
        for asset in assets:
            for strategy in strategies:
                try:
                    task = strategy_learning_scope_job.apply_async(
                        args=[asset.symbol, strategy.strategy_type, max_candidates],
                        queue="learning",
                    )
                    queued.append({"symbol": asset.symbol, "strategy": strategy.strategy_type, "task_id": task.id, "status": "queued"})
                except Exception as exc:
                    queued.append(
                        {
                            "symbol": asset.symbol,
                            "strategy": strategy.strategy_type,
                            "task_id": "",
                            "status": "unavailable",
                            "error_type": exc.__class__.__name__,
                            "error": str(exc),
                        }
                    )
        unavailable = sum(1 for item in queued if item["status"] == "unavailable")
        return {
            "status": "queued" if unavailable == 0 else "partially_queued" if unavailable < len(queued) else "unavailable",
            "job": "strategy_learning_batch_job",
            "queued_scopes": len(queued) - unavailable,
            "unavailable_scopes": unavailable,
            "symbols": [asset.symbol for asset in assets],
            "strategies": [strategy.strategy_type for strategy in strategies],
            "tasks": queued[:50],
            "paper_only": True,
            "note": "Scoped learning jobs run parameter experiments only; they do not apply promotions or execute trades.",
        }

    return _run_job("strategy_learning_batch_job", work)


@celery_app.task
def retry_failed_strategy_learning_scopes_job() -> dict:
    """Requeue aged transient scope failures without waiting for the next batch."""
    def work(db):
        now = datetime.utcnow()
        cooldown = now - timedelta(minutes=15)
        alerts = db.query(Notification).filter(
            Notification.category == "scheduled_job",
            Notification.severity == "critical",
            Notification.status == "open",
            Notification.source.like("strategy_learning_scope_job:%"),
            Notification.created_at <= cooldown,
        ).order_by(Notification.created_at, Notification.id).limit(32).all()
        queued = []
        for alert in alerts:
            source = alert.source.removeprefix("strategy_learning_scope_job:")
            if ":" not in source:
                continue
            symbol, strategy_slug = source.split(":", 1)
            payload = dict(alert.payload or {})
            retry_queued_at = payload.get("retry_queued_at")
            if retry_queued_at:
                try:
                    if datetime.fromisoformat(str(retry_queued_at)) > cooldown:
                        continue
                except ValueError:
                    pass
            task = strategy_learning_scope_job.apply_async(
                # Recovery should prove the database/worker path first. The
                # regular batch job remains responsible for broader candidate
                # exploration once the scope is healthy again.
                args=[symbol, strategy_slug, 1], queue="learning", expires=15 * 60
            )
            payload.update({
                "retry_queued_at": now.isoformat(),
                "retry_task_id": task.id,
            })
            alert.payload = payload
            alert.updated_at = now
            queued.append({"source": alert.source, "task_id": task.id})
        db.commit()
        return {
            "status": "complete",
            "job": "retry_failed_strategy_learning_scopes_job",
            "queued": queued,
            "paper_only": True,
        }

    return _run_job("retry_failed_strategy_learning_scopes_job", work)


@celery_app.task
def nightly_strategy_learning_job() -> dict:
    def work(db):
        return {"status": "quarantined", "job": "nightly_strategy_learning_job",
                "reason": "Legacy PaperTrade/StrategyMemory evidence is nonqualifying for stock-paper execution."}

    return _run_job("nightly_strategy_learning_job", work)


@celery_app.task
def trade_candidate_scan_job() -> dict:
    def work(db):
        snapshot = get_trade_candidate_snapshot(db, limit=12, refresh=True)
        return {
            "status": "complete",
            "job": "trade_candidate_scan_job",
            "candidate_count": snapshot["candidate_count"],
            "positive_count": snapshot["positive_count"],
            "cache_status": snapshot["cache_status"],
        }

    return _run_job("trade_candidate_scan_job", work)


@celery_app.task
def paper_trading_signal_job() -> dict:
    def work(db):
        return {"status": "quarantined", "job": "paper_trading_signal_job",
                "reason": "Legacy local simulator cannot create stock-paper orders."}

    return _run_job("paper_trading_signal_job", work)


@celery_app.task
def paper_trade_reconciliation_job() -> dict:
    def work(db):
        return {"status": "quarantined", "job": "paper_trade_reconciliation_job",
                "reason": "Legacy simulator outcomes cannot update qualifying stock-paper evidence."}

    return _run_job("paper_trade_reconciliation_job", work)


@celery_app.task
def stock_paper_broker_reconciliation_job() -> dict:
    """Refresh the broker-observed stock-paper ledger without placing orders."""
    def work(db):
        from app.services.stock_paper_ledger import (
            active_paper_account,
            active_paper_broker_name,
            reconcile_stock_paper_account,
        )

        account = active_paper_account(db)
        if account is None:
            return {
                "status": "skipped",
                "job": "stock_paper_broker_reconciliation_job",
                "reason": "stock paper account has not been explicitly initialized",
                "paper_only": True,
            }
        if active_paper_broker_name() != "alpaca_paper":
            return {
                "status": "skipped",
                "job": "stock_paper_broker_reconciliation_job",
                "reason": "automatic stock-paper reconciliation is limited to Alpaca paper evidence",
                "paper_only": True,
            }
        result = reconcile_stock_paper_account(db)
        return {
            "status": "complete",
            "job": "stock_paper_broker_reconciliation_job",
            "paper_only": True,
            "account_status": result.get("account_status"),
            "reconciliation_required": result.get("reconciliation_required"),
        }

    return _run_job("stock_paper_broker_reconciliation_job", work)


@celery_app.task
def memory_replay_gate_monitor_job() -> dict:
    def work(db):
        return {"status": "quarantined", "job": "memory_replay_gate_monitor_job",
                "reason": "Legacy memory replay cannot mutate stock-paper eligibility."}

    return _run_job("memory_replay_gate_monitor_job", work)


@celery_app.task
def deployment_monitor_job() -> dict:
    def work(db):
        return {"job": "deployment_monitor_job", **run_deployment_monitor(db, source="deployment_monitor_job")}

    return _run_job("deployment_monitor_job", work)


@celery_app.task
def strategy_promotion_job() -> dict:
    def work(db):
        return {"status": "quarantined", "job": "strategy_promotion_job",
                "reason": "Legacy governance must not mutate stock-paper strategy eligibility."}

    return _run_job("strategy_promotion_job", work)


@celery_app.task
def risk_monitor_job() -> dict:
    def work(db):
        return {"status": "quarantined", "job": "risk_monitor_job",
                "reason": "Legacy paper-trade risk monitoring cannot mutate stock-paper kill or strategy state."}

    return _run_job("risk_monitor_job", work)


@celery_app.task
def stock_monitoring_job() -> dict:
    def work(db):
        result = run_stock_monitoring(db)
        sync_cycle_observability(
            db,
            monitor_snapshot_id=result.get("snapshot_id"),
            recovery_event_id=result.get("recovery_event_id"),
        )
        db.commit()
        return result
    return _run_job("stock_monitoring_job", work)


@celery_app.task
def live_broker_reconciliation_job() -> dict:
    """Continuously reconcile live broker truth; absence of a live account is safe."""
    def work(db):
        from app.models import LiveBrokerAccount

        if db.query(LiveBrokerAccount).filter_by(broker="alpaca_live").one_or_none() is None:
            return {
                "status": "skipped",
                "job": "live_broker_reconciliation_job",
                "reason": "no live broker account has been initialized",
            }
        try:
            return reconcile_live_broker_account(db)
        except LiveBrokerError as exc:
            return {
                "status": "halted",
                "job": "live_broker_reconciliation_job",
                "reason": str(exc),
            }

    return _run_job("live_broker_reconciliation_job", work)

@celery_app.task
def stock_training_job(job_id: str) -> dict:
    """Execute a persisted stock-training intent; Redis state is never queried."""
    db = SessionLocal()
    try:
        result = run_stock_training_job(db, job_id)
        cycle = sync_cycle_from_training_job(db, job_id)
        db.commit()
        if cycle:
            result["cycle_id"] = cycle.cycle_id
        return result
    finally:
        db.close()


@celery_app.task(
    soft_time_limit=45,
    time_limit=55,
)
def agent_research_job(run_id: str) -> dict:
    """Run one bounded research-only agent request with no broker tools."""
    # Research runs are independent by symbol. Scope the advisory lease to the
    # immutable run ID so one slow model call cannot serialize the whole
    # multi-symbol learning queue or cause other queued runs to be discarded.
    return _run_job(
        f"agent_research_job:{run_id}",
        lambda db: run_agent_research(db, run_id),
    )

@celery_app.task
def recover_stock_training_jobs_job() -> dict:
    def work(db):
        recovered = recover_stock_training_jobs(db)
        db.commit()
        return {
            "status": "complete",
            "job": "recover_stock_training_jobs_job",
            "recovered_job_ids": [item.id for item in recovered],
            "paper_only": True,
            "live_authorized": False,
        }

    return _run_job("recover_stock_training_jobs_job", work)

@celery_app.task
def scheduled_stock_challenger_retraining_job() -> dict:
    """Schedule challengers only; this task never creates a paper binding."""
    def work(db):
        control = scheduled_learning_control_projection(db)
        if control["paused"]:
            return {
                "status": "paused",
                "job": "scheduled_stock_challenger_retraining_job",
                "reason": control["pause_reason"],
                "challengers": [],
                "deferred": [],
                "blocked": [],
                "paper_only": True,
                "live_authorized": False,
                "binding_changed": False,
            }
        symbols = list(settings.stock_learning_default_symbols)
        active_symbols = {
            row.symbol for row in db.query(Asset).filter(
                Asset.is_active.is_(True), Asset.asset_type == "stock",
                Asset.symbol.in_(symbols),
            ).all()
        }
        missing = sorted(set(symbols) - active_symbols)
        if missing:
            return {
                "status": "blocked", "job": "scheduled_stock_challenger_retraining_job",
                "reason": "Configured learning universe contains missing or inactive assets",
                "missing_symbols": missing, "challengers": [], "deferred": [], "blocked": [],
                "paper_only": True, "live_authorized": False, "binding_changed": False,
            }
        queued, deferred, blocked = [], [], []
        # One immutable portfolio cycle shares the launch universe. Independent
        # symbol/strategy experiments remain on the research-only batch path.
        try:
            cycle, duplicate = create_learning_cycle(
                db, symbols=symbols, cutoff_at=date.today(), horizon_days=5,
                provider=settings.stock_learning_default_provider,
                seed=42, actor="scheduler", trigger="scheduled"
            )
            db.commit()
            if cycle.training_job_id and not duplicate and cycle.status == "queued":
                job = db.get(StockTrainingJob, cycle.training_job_id)
                if job:
                    enqueue_stock_training_job(db, job)
                db.commit()
            item = {"symbols": symbols, "cycle_id": cycle.cycle_id, "job_id": cycle.training_job_id,
                    "status": cycle.status, "stage": cycle.stage, "deduplicated": duplicate,
                    "reason": cycle.last_reason}
            (deferred if cycle.status == "deferred" else blocked if cycle.status == "blocked" else queued).append(item)
        except StockTrainingError as exc:
            db.rollback()
            blocked.append({"symbols": symbols, "reason": str(exc)})
        return {
            "status": "complete",
            "job": "scheduled_stock_challenger_retraining_job",
            "challengers": queued,
            "deferred": deferred,
            "blocked": blocked,
            "paper_only": True,
            "live_authorized": False,
            "binding_changed": False,
        }

    return _run_job("scheduled_stock_challenger_retraining_job", work)


@celery_app.task
def scheduled_stock_paper_trial_handoff_job() -> dict:
    """Admit and safely start completed scheduled challengers."""
    return _run_job(
        "scheduled_stock_paper_trial_handoff_job",
        lambda db: run_scheduled_paper_trial_handoff_job(db),
    )


@celery_app.task
def scheduled_stock_paper_promotion_job() -> dict:
    """Evaluate completed scheduled challengers; promote paper canaries only."""
    return _run_job(
        "scheduled_stock_paper_promotion_job",
        lambda db: run_automatic_paper_promotion_job(db),
    )

@celery_app.task
def stock_forward_trial_observe_job(trial_id: str | None = None) -> dict:
    """Process explicitly running trials; approval alone never starts execution."""
    def work(db):
        rows = (
            [db.get(StockPaperTrial, trial_id)]
            if trial_id
            else db.query(StockPaperTrial).filter(StockPaperTrial.status != "completed").all()
        )
        if trial_id and not rows[0]:
            return {"status": "missing", "trial_id": trial_id}
        results = []
        for row in rows:
            if row:
                observe_trial(db, row.id)
                execute_pending_decisions(db, row.id)
                # Commit the observation and any broker-intent changes before
                # calculating numeric evidence. A malformed metric payload must
                # not erase the durable observation that produced it.
                _normalize_pending_json(db)
                db.commit()
                try:
                    metric = evaluate_trial(db, row.id)
                    _normalize_pending_json(db)
                    db.flush()
                    db.commit()
                    classification = metric.classification
                    metric_error = None
                except Exception as exc:
                    db.rollback()
                    classification = "unavailable"
                    metric_error = f"{exc.__class__.__name__}: {exc}"
                db.refresh(row)
                sync_cycle_from_trial(db, row.id, actor="forward_trial_worker")
                db.commit()
                result = {"trial_id": row.id, "status": row.status, "classification": classification}
                if metric_error:
                    result["metric_error"] = metric_error
                results.append(result)
        return {"status": "complete", "trials": results, "paper_only": True}
    return _run_job("stock_forward_trial_observe_job", work)

@celery_app.task
def stock_forward_trial_reconcile_job() -> dict:
    return _run_job("stock_forward_trial_reconcile_job", reconcile_inflight_stock_orders_on_restart)

@celery_app.task
def stock_forward_trial_evaluate_job(trial_id: str) -> dict:
    def work(db):
        row = db.get(StockPaperTrial, trial_id)
        if not row:
            return {"status": "missing", "trial_id": trial_id}
        metric = evaluate_trial(db, trial_id)
        db.commit()
        sync_cycle_from_trial(db, trial_id, report_id=None, actor="forward_trial_worker")
        db.commit()
        return {"status": metric.classification, "trial_id": trial_id, **metric.payload}
    return _run_job("stock_forward_trial_evaluate_job", work)
