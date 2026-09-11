from __future__ import annotations

from typing import Callable

from app.db.session import SessionLocal
from app.models import Asset, PaperTrade, Strategy
from app.services.economic_data import import_fallback_economic_indicators
from app.services.decision_journal import refresh_decision_journal_outcomes, update_strategy_memory_from_journal
from app.services.deployment_monitor import run_deployment_monitor
from app.services.experiments import run_strategy_experiments
from app.services.governance import evaluate_strategy_governance
from app.services.market_data import import_market_prices
from app.services.market_regime import detect_and_store_market_regime
from app.services.memory_replay import run_memory_replay_gate_monitor
from app.services.model_tracking import run_and_persist_model_predictions, score_realized_predictions
from app.services.news_sentiment import import_mock_news
from app.services.notifications import create_notification
from app.services.paper_trading import reconcile_open_paper_trades, run_paper_signal, update_all_strategy_memory
from app.services.risk_actions import evaluate_portfolio_risk_actions
from app.services.trade_candidates import get_trade_candidate_snapshot
from app.tasks.celery_app import celery_app


def _run_job(job_name: str, work: Callable) -> dict:
    db = SessionLocal()
    try:
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
        db.close()


@celery_app.task
def daily_market_data_import() -> dict:
    def work(db):
        assets = db.query(Asset).filter(Asset.is_active.is_(True)).order_by(Asset.symbol).all()
        results = [import_market_prices(db, asset.symbol, "2y") for asset in assets]
        journal = refresh_decision_journal_outcomes(db, source="daily_market_data_import", notify=True)
        replay_monitor = run_memory_replay_gate_monitor(db, source="daily_market_data_import", limit=60, top_k=3)
        return {
            "status": "complete",
            "job": "daily_market_data_import",
            "results": results,
            "decision_journal": journal,
            "memory_replay_gate_monitor": replay_monitor,
        }

    return _run_job("daily_market_data_import", work)


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
        updated = update_all_strategy_memory(db)
        journal_memory = update_strategy_memory_from_journal(db)
        return {"status": "complete", "job": "nightly_strategy_learning_job", "memory_rows_updated": updated, "journal_memory": journal_memory}

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
        snapshot = get_trade_candidate_snapshot(db, limit=10, refresh=False)
        candidates = [
            candidate
            for candidate in snapshot["candidates"]
            if candidate["candidate_status"] == "positive_candidate" and candidate["strategy_status"] == "paper_trading_active"
        ]
        selected = []
        skipped_open = []
        for candidate in candidates:
            strategy = db.query(Strategy).filter(Strategy.strategy_type == candidate["strategy"]).one_or_none()
            has_open_trade = (
                db.query(PaperTrade)
                .filter(
                    PaperTrade.symbol == candidate["symbol"],
                    PaperTrade.strategy_id == (strategy.id if strategy else None),
                    PaperTrade.status == "open",
                )
                .count()
                > 0
            )
            if has_open_trade:
                skipped_open.append({"symbol": candidate["symbol"], "strategy": candidate["strategy"]})
                continue
            selected.append(candidate)
            if len(selected) >= 3:
                break
        results = [run_paper_signal(db, candidate["symbol"], candidate["strategy"]) for candidate in selected]
        return {
            "status": "complete" if results else "skipped",
            "job": "paper_trading_signal_job",
            "source": "trade_candidate_snapshot",
            "candidate_count": snapshot["candidate_count"],
            "positive_count": snapshot["positive_count"],
            "selected_candidates": [
                {"symbol": candidate["symbol"], "strategy": candidate["strategy"], "score": candidate["score"]}
                for candidate in selected
            ],
            "skipped_open": skipped_open,
            "results": results,
        }

    return _run_job("paper_trading_signal_job", work)


@celery_app.task
def paper_trade_reconciliation_job() -> dict:
    def work(db):
        result = reconcile_open_paper_trades(db)
        journal = refresh_decision_journal_outcomes(db, source="paper_trade_reconciliation_job", notify=True)
        replay_monitor = run_memory_replay_gate_monitor(db, source="paper_trade_reconciliation_job", limit=60, top_k=3)
        return {"status": "complete", "job": "paper_trade_reconciliation_job", **result, "decision_journal": journal, "memory_replay_gate_monitor": replay_monitor}

    return _run_job("paper_trade_reconciliation_job", work)


@celery_app.task
def memory_replay_gate_monitor_job() -> dict:
    def work(db):
        result = run_memory_replay_gate_monitor(db, source="memory_replay_gate_monitor_job", limit=60, top_k=3)
        return {"status": "complete", "job": "memory_replay_gate_monitor_job", **result}

    return _run_job("memory_replay_gate_monitor_job", work)


@celery_app.task
def deployment_monitor_job() -> dict:
    def work(db):
        return {"job": "deployment_monitor_job", **run_deployment_monitor(db, source="deployment_monitor_job")}

    return _run_job("deployment_monitor_job", work)


@celery_app.task
def strategy_promotion_job() -> dict:
    def work(db):
        result = evaluate_strategy_governance(db)
        return {"status": "complete", "job": "strategy_promotion_job", **result}

    return _run_job("strategy_promotion_job", work)


@celery_app.task
def risk_monitor_job() -> dict:
    def work(db):
        model_scores = score_realized_predictions(db)
        portfolio_actions = evaluate_portfolio_risk_actions(db)
        return {"status": "complete", "job": "risk_monitor_job", "model_scores": model_scores, "portfolio_actions": portfolio_actions}

    return _run_job("risk_monitor_job", work)
