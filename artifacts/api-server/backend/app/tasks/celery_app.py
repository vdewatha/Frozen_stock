from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab
from kombu import Exchange, Queue

from app.core.config import settings

INTRADAY_MARKET_DATA_QUEUE = "intraday_market_data"
RESEARCH_MARKET_DATA_QUEUE = "research_market_data"
SCALP_RESEARCH_QUEUE = "scalp_research"
MONITORING_QUEUE = "monitoring"
GENERAL_WORKER_QUEUES = frozenset({"default", "market_data", "learning", "paper_trading", "risk"})
OBSERVATION_TASKS = frozenset({
    "app.tasks.jobs.iex_research_collection_job",
    "app.tasks.jobs.delayed_sip_collection_job",
})


def configured_schedule(schedule: dict, *, observation_only: bool) -> dict:
    if not observation_only:
        return schedule
    return {name: entry for name, entry in schedule.items() if entry["task"] in OBSERVATION_TASKS}

celery_app = Celery("trading_app", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.timezone = "America/New_York"
celery_app.conf.task_track_started = True
celery_app.conf.worker_send_task_events = True
celery_app.conf.task_send_sent_event = True
celery_app.conf.task_queues = (
    Queue("default", Exchange("default"), routing_key="default"),
    Queue("market_data", Exchange("market_data"), routing_key="market_data"),
    Queue(INTRADAY_MARKET_DATA_QUEUE, Exchange(INTRADAY_MARKET_DATA_QUEUE), routing_key=INTRADAY_MARKET_DATA_QUEUE),
    Queue(RESEARCH_MARKET_DATA_QUEUE, Exchange(RESEARCH_MARKET_DATA_QUEUE), routing_key=RESEARCH_MARKET_DATA_QUEUE),
    Queue(SCALP_RESEARCH_QUEUE, Exchange(SCALP_RESEARCH_QUEUE), routing_key=SCALP_RESEARCH_QUEUE),
    Queue("learning", Exchange("learning"), routing_key="learning"),
    Queue("paper_trading", Exchange("paper_trading"), routing_key="paper_trading"),
    Queue("risk", Exchange("risk"), routing_key="risk"),
    Queue(MONITORING_QUEUE, Exchange(MONITORING_QUEUE), routing_key=MONITORING_QUEUE),
)
celery_app.conf.task_default_queue = "default"
celery_app.conf.worker_prefetch_multiplier = 1
celery_app.conf.task_routes = {
    "app.tasks.jobs.delayed_sip_collection_job": {"queue": RESEARCH_MARKET_DATA_QUEUE},
    "app.tasks.jobs.iex_research_collection_job": {"queue": RESEARCH_MARKET_DATA_QUEUE},
    "app.tasks.jobs.agent_research_job": {"queue": RESEARCH_MARKET_DATA_QUEUE},
    "app.tasks.jobs.scalp_research_job": {"queue": SCALP_RESEARCH_QUEUE},
    "app.tasks.jobs.daily_market_data_import": {"queue": "market_data"},
    "app.tasks.jobs.intraday_market_data_import": {"queue": INTRADAY_MARKET_DATA_QUEUE},
    "app.tasks.jobs.daily_economic_data_import": {"queue": "market_data"},
    "app.tasks.jobs.daily_news_import": {"queue": "market_data"},
    "app.tasks.jobs.daily_feature_generation": {"queue": "market_data"},
    "app.tasks.jobs.model_realization_scoring_job": {"queue": "learning"},
    "app.tasks.jobs.daily_market_regime_detection": {"queue": "market_data"},
    "app.tasks.jobs.trade_candidate_scan_job": {"queue": "market_data"},
    "app.tasks.jobs.nightly_backtest_job": {"queue": "learning"},
    "app.tasks.jobs.nightly_strategy_learning_job": {"queue": "learning"},
    "app.tasks.jobs.strategy_learning_scope_job": {"queue": "learning"},
    "app.tasks.jobs.strategy_learning_batch_job": {"queue": "learning"},
    "app.tasks.jobs.recover_unpublished_strategy_learning_scopes_job": {"queue": "learning"},
    "app.tasks.jobs.retry_failed_strategy_learning_scopes_job": {"queue": "learning"},
    "app.tasks.jobs.paper_trading_signal_job": {"queue": "paper_trading"},
    "app.tasks.jobs.paper_trade_reconciliation_job": {"queue": "paper_trading"},
    "app.tasks.jobs.stock_paper_broker_reconciliation_job": {"queue": "paper_trading"},
    "app.tasks.jobs.memory_replay_gate_monitor_job": {"queue": "learning"},
    "app.tasks.jobs.deployment_monitor_job": {"queue": "risk"},
    "app.tasks.jobs.strategy_promotion_job": {"queue": "risk"},
    "app.tasks.jobs.risk_monitor_job": {"queue": "risk"},
    "app.tasks.jobs.stock_monitoring_job": {"queue": MONITORING_QUEUE},
    "app.tasks.jobs.live_broker_reconciliation_job": {"queue": "risk"},
    "app.tasks.jobs.stock_training_job": {"queue": "learning"},
    "app.tasks.jobs.recover_stock_training_jobs_job": {"queue": "learning"},
    "app.tasks.jobs.scheduled_stock_challenger_retraining_job": {"queue": "learning"},
    "app.tasks.jobs.scheduled_stock_paper_trial_handoff_job": {"queue": "learning"},
    "app.tasks.jobs.scheduled_stock_paper_promotion_job": {"queue": "learning"},
    "app.tasks.jobs.stock_forward_trial_observe_job": {"queue": "paper_trading"},
    "app.tasks.jobs.stock_forward_trial_reconcile_job": {"queue": "paper_trading"},
    "app.tasks.jobs.stock_forward_trial_evaluate_job": {"queue": "paper_trading"},
}
celery_app.conf.beat_schedule = {
    "delayed-sip-collection": {
        "task": "app.tasks.jobs.delayed_sip_collection_job",
        "schedule": 300,
        "options": {"expires": 240},
    },
    "iex-research-collection": {
        "task": "app.tasks.jobs.iex_research_collection_job",
        "schedule": crontab(),
        "options": {"expires": 55},
    },
    "daily-market-data-import": {"task": "app.tasks.jobs.daily_market_data_import", "schedule": 60 * 60 * 24},
    # Expire a tick before the next cadence window. The task itself also holds
    # a Redis lease, so a slow request is skipped rather than overlapped.
    "intraday-market-data-import": {
        "task": "app.tasks.jobs.intraday_market_data_import",
        # Readiness advances at minute boundaries, independent of beat startup.
        "schedule": crontab(),
        "options": {"expires": 55},
    },
    "daily-economic-data-import": {"task": "app.tasks.jobs.daily_economic_data_import", "schedule": 60 * 60 * 24},
    "daily-news-import": {"task": "app.tasks.jobs.daily_news_import", "schedule": 60 * 60 * 24},
    # Re-run the corrected, trusted prediction pipeline often enough to clear
    # transient worker failures and keep the multi-symbol research loop fresh.
    # It remains serialized by the job lease and never authorizes orders.
    "daily-feature-generation": {"task": "app.tasks.jobs.daily_feature_generation", "schedule": 60 * 60 * 6},
    # Prediction labels mature on trading-day horizons. Re-score often enough
    # to feed the learning and monitoring views without overlapping imports.
    "model-realization-scoring": {
        "task": "app.tasks.jobs.model_realization_scoring_job",
        # Matured one-day forecasts should enter the feedback loop within an
        # hour of their next trusted daily bar, without creating any orders.
        "schedule": 60 * 60,
    },
    "trade-candidate-scan-job": {"task": "app.tasks.jobs.trade_candidate_scan_job", "schedule": 60 * 60 * 6},
    "daily-market-regime-detection": {"task": "app.tasks.jobs.daily_market_regime_detection", "schedule": 60 * 60 * 24},
    "nightly-backtest-job": {"task": "app.tasks.jobs.nightly_backtest_job", "schedule": 60 * 60 * 24},
    "nightly-learning-job": {"task": "app.tasks.jobs.nightly_strategy_learning_job", "schedule": 60 * 60 * 24},
    "strategy-learning-batch-job": {"task": "app.tasks.jobs.strategy_learning_batch_job", "schedule": 60 * 60 * 6},
    "strategy-learning-dispatch-recovery": {
        "task": "app.tasks.jobs.recover_unpublished_strategy_learning_scopes_job",
        "schedule": 60,
    },
    "strategy-learning-failure-retry": {
        "task": "app.tasks.jobs.retry_failed_strategy_learning_scopes_job",
        # The task applies a per-alert 15-minute cooldown, so a minutely
        # check reduces recovery latency without duplicating scope work.
        "schedule": 60,
    },
    "paper-trading-signal-job": {"task": "app.tasks.jobs.paper_trading_signal_job", "schedule": 60 * 60},
    "paper-trade-reconciliation-job": {"task": "app.tasks.jobs.paper_trade_reconciliation_job", "schedule": 60 * 30},
    "stock-paper-broker-reconciliation-job": {
        "task": "app.tasks.jobs.stock_paper_broker_reconciliation_job",
        "schedule": 60 * 5,
    },
    "memory-replay-gate-monitor-job": {"task": "app.tasks.jobs.memory_replay_gate_monitor_job", "schedule": 60 * 30},
    "deployment-monitor-job": {"task": "app.tasks.jobs.deployment_monitor_job", "schedule": 60 * 15},
    "strategy-promotion-job": {"task": "app.tasks.jobs.strategy_promotion_job", "schedule": 60 * 60 * 24},
    "risk-monitor-job": {"task": "app.tasks.jobs.risk_monitor_job", "schedule": 60 * 5},
    "stock-monitoring-job": {
        "task": "app.tasks.jobs.stock_monitoring_job",
        "schedule": 60 * 5,
        # Keep monitoring isolated from risk traffic and discard an overdue
        # snapshot rather than allowing stale evidence to queue indefinitely.
        "options": {"queue": MONITORING_QUEUE, "expires": 240},
    },
    "live-broker-reconciliation-job": {
        "task": "app.tasks.jobs.live_broker_reconciliation_job",
        "schedule": 60,
        "options": {"expires": 55},
    },
    "stock-training-recovery-job": {"task": "app.tasks.jobs.recover_stock_training_jobs_job", "schedule": 60 * 5},
    "scheduled-stock-challenger-retraining": {"task": "app.tasks.jobs.scheduled_stock_challenger_retraining_job", "schedule": 60 * 60 * 24},
    "scheduled-stock-paper-trial-handoff": {"task": "app.tasks.jobs.scheduled_stock_paper_trial_handoff_job", "schedule": 60 * 5},
    "scheduled-stock-paper-promotion": {"task": "app.tasks.jobs.scheduled_stock_paper_promotion_job", "schedule": 60 * 15},
    "stock-forward-trial-reconcile": {
        "task": "app.tasks.jobs.stock_forward_trial_reconcile_job",
        "schedule": 60,
        "options": {"expires": 55},
    },
    "stock-forward-trial-observe": {
        "task": "app.tasks.jobs.stock_forward_trial_observe_job",
        "schedule": 60,
        "options": {"expires": 55},
    },
}

celery_app.conf.beat_schedule = configured_schedule(
    celery_app.conf.beat_schedule, observation_only=settings.research_observation_only,
)
# Production uses a separately supervised monitor loop so a beat restart cannot
# leave the recovery heartbeat stale. Keep the Celery schedule for local stacks
# and deployments that do not opt into the direct loop.
if os.environ.get("PAPER_DIRECT_MONITOR_LOOP", "false").strip().lower() == "true":
    celery_app.conf.beat_schedule.pop("stock-monitoring-job", None)
# The task definitions live in `app.tasks.jobs`, rather than the conventional
# `app.tasks.tasks` module. Explicitly name that related module so standalone
# Celery workers register the same tasks as the API process.
celery_app.autodiscover_tasks(["app.tasks"], related_name="jobs")
