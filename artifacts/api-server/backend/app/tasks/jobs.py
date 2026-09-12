from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
from decimal import Decimal
from typing import Callable

from sqlalchemy import text
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.types import JSON

from app.db.session import SessionLocal
from app.models import Asset, PaperTrade, Strategy
from app.services.economic_data import import_fallback_economic_indicators
from app.services.decision_journal import refresh_decision_journal_outcomes, update_strategy_memory_from_journal
from app.services.deployment_monitor import run_deployment_monitor
from app.services.experiments import run_strategy_experiments
from app.services.governance import evaluate_strategy_governance
from app.services.market_data import import_market_prices
from app.services.intraday_data import ingest_corporate_actions, ingest_intraday
from app.services.market_regime import detect_and_store_market_regime
from app.services.memory_replay import run_memory_replay_gate_monitor
from app.services.model_tracking import run_and_persist_model_predictions, score_realized_predictions
from app.services.news_sentiment import import_mock_news
from app.services.notifications import create_notification
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
from app.services.stock_forward_trial import observe_trial, execute_pending_decisions, start_trial, evaluate_trial
from app.models import StockPaperTrial
from app.tasks.celery_app import celery_app


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


def _run_job(job_name: str, work: Callable) -> dict:
    db = SessionLocal()
    lock_key = int.from_bytes(sha256(f"job:{job_name}".encode()).digest()[:8], "big") % 2_147_483_647
    lock_acquired = False
    try:
        if db.get_bind().dialect.name == "postgresql":
            lock_acquired = bool(db.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}))
            if not lock_acquired:
                return {"status": "skipped", "job": job_name, "reason": "duplicate worker lease is active"}
        return work(db)
    except Exception as exc:
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
        raise
    finally:
        if lock_acquired:
            try:
                db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
            except Exception:
                db.rollback()
        db.close()


@celery_app.task
def daily_market_data_import() -> dict:
    def work(db):
        assets = db.query(Asset).filter(Asset.is_active.is_(True)).order_by(Asset.symbol).all()
        results = [import_market_prices(db, asset.symbol, "2y") for asset in assets]
        corporate_actions = ingest_corporate_actions(
            db, [asset.symbol for asset in assets if asset.symbol in {"AAPL", "MSFT", "QQQ", "SPY"}]
        )
        journal = refresh_decision_journal_outcomes(db, source="daily_market_data_import", notify=True)
        replay_monitor = run_memory_replay_gate_monitor(db, source="daily_market_data_import", limit=60, top_k=3)
        return {
            "status": "complete",
            "job": "daily_market_data_import",
            "results": results,
            "corporate_actions": corporate_actions,
            "decision_journal": journal,
            "memory_replay_gate_monitor": replay_monitor,
        }

    return _run_job("daily_market_data_import", work)

@celery_app.task
def intraday_market_data_import() -> dict:
    return _run_job("intraday_market_data_import", lambda db: ingest_intraday(db))


@celery_app.task
def daily_news_import() -> dict:
    def work(db):
        assets = db.query(Asset).filter(Asset.is_active.is_(True)).order_by(Asset.symbol).limit(10).all()
        results = [import_mock_news(db, asset.symbol) for asset in assets]
        return {"status": "complete", "job": "daily_news_import", "results": results}

    return _run_job("daily_news_import", work)


@celery_app.task
def daily_feature_generation() -> dict:
    def work(db):
        assets = db.query(Asset).filter(Asset.is_active.is_(True)).order_by(Asset.symbol).limit(10).all()
        results = [run_and_persist_model_predictions(db, asset.symbol) for asset in assets]
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
        assets = db.query(Asset).filter(Asset.is_active.is_(True)).order_by(Asset.symbol).limit(5).all()
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


@celery_app.task
def strategy_learning_scope_job(symbol: str, strategy_slug: str, max_candidates: int = 3) -> dict:
    def work(db):
        result = run_strategy_experiments(
            db,
            symbol=symbol,
            strategy_slug=strategy_slug,
            max_candidates=max_candidates,
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
def strategy_learning_batch_job(limit_symbols: int = 8, limit_strategies: int = 8, max_candidates: int = 3) -> dict:
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
    return _run_job("stock_monitoring_job", lambda db: run_stock_monitoring(db))

@celery_app.task
def stock_training_job(job_id: str) -> dict:
    """Execute a persisted stock-training intent; Redis state is never queried."""
    db = SessionLocal()
    try:
        return run_stock_training_job(db, job_id)
    finally:
        db.close()

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
        assets = db.query(Asset).filter(Asset.is_active.is_(True), Asset.asset_type == "stock").order_by(Asset.symbol).limit(5).all()
        queued, deferred, blocked = [], [], []
        for asset in assets:
            try:
                job, duplicate = create_stock_training_job(
                    db, symbols=[asset.symbol], cutoff_at=date.today(), horizon_bars=5, seed=42,
                    actor="scheduler", trigger="scheduled"
                )
                db.commit()
                if not duplicate and job.status == "queued":
                    enqueue_stock_training_job(db, job)
                    db.commit()
                item = {"symbol": asset.symbol, "job_id": job.id, "status": job.status, "deduplicated": duplicate}
                (deferred if job.status == "deferred" else queued).append(item)
            except StockTrainingError as exc:
                db.rollback()
                blocked.append({"symbol": asset.symbol, "reason": str(exc)})
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
                metric = evaluate_trial(db, row.id)
                results.append({"trial_id": row.id, "status": row.status, "classification": metric.classification})
        _normalize_pending_json(db)
        db.commit()
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
        return {"status": metric.classification, "trial_id": trial_id, **metric.payload}
    return _run_job("stock_forward_trial_evaluate_job", work)
