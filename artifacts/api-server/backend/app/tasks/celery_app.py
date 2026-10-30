from __future__ import annotations

import json
import os
import socket

from celery import Celery
from celery.schedules import crontab
from celery.signals import heartbeat_sent, worker_ready
from kombu import Exchange, Queue
import redis

from app.core.config import settings

INTRADAY_MARKET_DATA_QUEUE = "intraday_market_data"
RESEARCH_MARKET_DATA_QUEUE = "research_market_data"
SCALP_RESEARCH_QUEUE = "scalp_research"
MONITORING_QUEUE = "monitoring"
# The five-minute broker reconciliation is the watchdog's broker heartbeat. It
# gets a lane of its own so the minutely signal pass cannot queue it past the
# watchdog's ten-minute staleness limit (which re-pauses recovery).
BROKER_RECONCILIATION_QUEUE = "broker_reconciliation"
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


def _record_worker_heartbeat(*, sender=None, **kwargs) -> None:
    """Publish short-lived evidence from the Celery worker process itself."""
    hostname = getattr(sender, "hostname", None) or kwargs.get("hostname") or socket.gethostname()
    queue_mode = os.environ.get("PAPER_WORKER_QUEUE_MODE", "").strip().lower()
    payload = json.dumps({"hostname": hostname, "queue_mode": queue_mode})
    client = None
    try:
        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        client.set(f"celery:worker:heartbeat:{hostname}", payload, ex=90)
    except Exception:
        # Worker health evidence is advisory and must never stop task execution.
        return
    finally:
        if client is not None:
            client.close()


heartbeat_sent.connect(_record_worker_heartbeat, weak=False)
worker_ready.connect(_record_worker_heartbeat, weak=False)
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
    Queue(BROKER_RECONCILIATION_QUEUE, Exchange(BROKER_RECONCILIATION_QUEUE), routing_key=BROKER_RECONCILIATION_QUEUE),
)
celery_app.conf.task_default_queue = "default"
celery_app.conf.worker_prefetch_multiplier = 1
celery_app.conf.task_routes = {
    "app.tasks.jobs.delayed_sip_collection_job": {"queue": RESEARCH_MARKET_DATA_QUEUE},
    "app.tasks.jobs.iex_research_collection_job": {"queue": RESEARCH_MARKET_DATA_QUEUE},
    "app.tasks.jobs.agent_research_job": {"queue": RESEARCH_MARKET_DATA_QUEUE},
    "app.tasks.jobs.scalp_research_job": {"queue": SCALP_RESEARCH_QUEUE},
    "app.tasks.jobs.scheduled_scalp_research_job": {"queue": SCALP_RESEARCH_QUEUE},
    "app.tasks.jobs.expire_stale_scalp_research_runs_job": {"queue": SCALP_RESEARCH_QUEUE},
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
    "app.tasks.jobs.stock_paper_broker_reconciliation_job": {"queue": BROKER_RECONCILIATION_QUEUE},
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
    "app.tasks.jobs.paper_trade_scorecard_snapshot_job": {"queue": "learning"},
    "app.tasks.jobs.operations_health_alert_job": {"queue": "risk"},
    "app.tasks.jobs.explained_halt_auto_resume_job": {"queue": "risk"},
    "app.tasks.jobs.strategy_readmission_job": {"queue": "learning"},
    "app.tasks.jobs.loss_feedback_job": {"queue": "learning"},
    "app.tasks.jobs.parameter_application_job": {"queue": "learning"},
    "app.tasks.jobs.alert_delivery_job": {"queue": "risk"},
    "app.tasks.jobs.alert_daily_digest_job": {"queue": "risk"},
    "app.tasks.jobs.paper_mandate_expiry_warning_job": {"queue": "learning"},
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
    # Slow jobs use wall-clock crontabs, not intervals. Beat's interval clock
    # restarts with every deploy or crash, so a 24-hour interval on a host that
    # restarts more than daily never fires (daily prices went stale for a week).
    # Times are America/New_York: imports land before the open and after the
    # close, and dependent jobs follow them.
    "daily-market-data-import": {"task": "app.tasks.jobs.daily_market_data_import", "schedule": crontab(minute=15, hour="8,17")},
    # Expire a tick before the next cadence window. The task itself also holds
    # a Redis lease, so a slow request is skipped rather than overlapped.
    "intraday-market-data-import": {
        "task": "app.tasks.jobs.intraday_market_data_import",
        # Readiness advances at minute boundaries, independent of beat startup.
        "schedule": crontab(),
        "options": {"expires": 55},
    },
    "daily-economic-data-import": {"task": "app.tasks.jobs.daily_economic_data_import", "schedule": crontab(minute=0, hour=7)},
    "daily-news-import": {"task": "app.tasks.jobs.daily_news_import", "schedule": crontab(minute=30, hour=7)},
    # Re-run the trusted prediction pipeline outside market hours only. A run
    # retrains every active symbol and took ~98 minutes on the 2 vCPU VM; the
    # 08:45 and 13:45 runs held the CPU through the open and starved the
    # minutely intraday import. Two runs keep predictions well inside the
    # 48-hour freshness gate. It is serialized by the job lease and never
    # authorizes orders.
    "daily-feature-generation": {"task": "app.tasks.jobs.daily_feature_generation", "schedule": crontab(minute=45, hour="2,17")},
    # Prediction labels mature on trading-day horizons. Re-score often enough
    # to feed the learning and monitoring views without overlapping imports.
    "model-realization-scoring": {
        "task": "app.tasks.jobs.model_realization_scoring_job",
        # Matured one-day forecasts should enter the feedback loop within an
        # hour of their next trusted daily bar, without creating any orders.
        "schedule": crontab(minute=50),
    },
    # The scanner fits models and runs backtests for every symbol and strategy;
    # its 09:00 and 14:00 runs competed for the CPU with the session's
    # minutely import and signal pass. It is research-only, so it runs off hours.
    "trade-candidate-scan-job": {"task": "app.tasks.jobs.trade_candidate_scan_job", "schedule": crontab(minute=0, hour="3,18")},
    "daily-market-regime-detection": {"task": "app.tasks.jobs.daily_market_regime_detection", "schedule": crontab(minute=0, hour=18)},
    "nightly-backtest-job": {"task": "app.tasks.jobs.nightly_backtest_job", "schedule": crontab(minute=0, hour=19)},
    "nightly-learning-job": {"task": "app.tasks.jobs.nightly_strategy_learning_job", "schedule": crontab(minute=30, hour=19)},
    "strategy-learning-batch-job": {"task": "app.tasks.jobs.strategy_learning_batch_job", "schedule": crontab(minute=0, hour="*/6")},
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
    # Keep the governed paper signal lane aligned with the one-minute scalp
    # research cadence. Reservation idempotency and risk gates prevent repeat
    # submissions; outside readiness it records a safe skip instead.
    # Drop a tick that waited past the next one instead of queueing a backlog.
    "paper-trading-signal-job": {
        "task": "app.tasks.jobs.paper_trading_signal_job",
        "schedule": 60,
        "options": {"expires": 55},
    },
    "paper-trade-reconciliation-job": {"task": "app.tasks.jobs.paper_trade_reconciliation_job", "schedule": 60 * 30},
    "stock-paper-broker-reconciliation-job": {
        "task": "app.tasks.jobs.stock_paper_broker_reconciliation_job",
        "schedule": 60 * 5,
        # A tick that waited past the next one is dropped, not run late.
        "options": {"expires": 4 * 60},
    },
    "memory-replay-gate-monitor-job": {"task": "app.tasks.jobs.memory_replay_gate_monitor_job", "schedule": 60 * 30},
    "deployment-monitor-job": {"task": "app.tasks.jobs.deployment_monitor_job", "schedule": 60 * 15},
    "strategy-promotion-job": {"task": "app.tasks.jobs.strategy_promotion_job", "schedule": crontab(minute=0, hour=20)},
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
    "scalp-research-queue-recovery": {
        "task": "app.tasks.jobs.expire_stale_scalp_research_runs_job",
        "schedule": 60 * 5,
        "options": {"expires": 240},
    },
    "scalp-research-batch": {
        "task": "app.tasks.jobs.scheduled_scalp_research_job",
        "schedule": 60 * 15,
        "options": {"expires": 10 * 60},
    },
    "scheduled-stock-challenger-retraining": {"task": "app.tasks.jobs.scheduled_stock_challenger_retraining_job", "schedule": crontab(minute=30, hour=18)},
    "scheduled-stock-paper-trial-handoff": {"task": "app.tasks.jobs.scheduled_stock_paper_trial_handoff_job", "schedule": 60 * 5},
    "scheduled-stock-paper-promotion": {"task": "app.tasks.jobs.scheduled_stock_paper_promotion_job", "schedule": 60 * 15},
    # Paper-only demotion of trial strategies with statistically confident
    # rolling edge decay. It can only pause; reactivation needs a new cycle.
    "strategy-edge-decay": {"task": "app.tasks.jobs.strategy_edge_decay_job", "schedule": 60 * 15},
    # Unattended paper operation: explained technical halts auto-resume once
    # every human-resume gate passes; suspended strategies get a governed
    # re-admission cycle after quarantine; alerts leave the app.
    "explained-halt-auto-resume": {
        "task": "app.tasks.jobs.explained_halt_auto_resume_job",
        "schedule": 60 * 5,
        "options": {"expires": 4 * 60},
    },
    "strategy-readmission": {"task": "app.tasks.jobs.strategy_readmission_job", "schedule": 60 * 15},
    # Paper loss feedback loop: attribute closed trades and review per-strategy
    # meta-label modes after the close (before the 19:00 loss-driven
    # experiments); governed parameter sets advance with their cycles.
    "loss-feedback": {
        "task": "app.tasks.jobs.loss_feedback_job",
        "schedule": crontab(minute=45, hour=16, day_of_week="mon-fri"),
        "options": {"expires": 2 * 60 * 60},
    },
    "parameter-application": {"task": "app.tasks.jobs.parameter_application_job", "schedule": 60 * 15},
    "alert-delivery": {
        "task": "app.tasks.jobs.alert_delivery_job",
        "schedule": 60,
        "options": {"expires": 55},
    },
    "alert-daily-digest": {
        "task": "app.tasks.jobs.alert_daily_digest_job",
        "schedule": crontab(minute=0, hour=17),
        "options": {"expires": 6 * 60 * 60},
    },
    "paper-mandate-expiry-warning": {
        "task": "app.tasks.jobs.paper_mandate_expiry_warning_job",
        "schedule": crontab(minute=5, hour="*/6"),
    },
    "stock-forward-trial-reconcile": {
        "task": "app.tasks.jobs.stock_forward_trial_reconcile_job",
        "schedule": 60,
        "options": {"expires": 55},
    },
    # Read-only daily scorecard snapshot after the close (weekdays, New York
    # wall clock) so the owner and the learning loop see each day's trades.
    "paper-trade-scorecard-snapshot": {
        "task": "app.tasks.jobs.paper_trade_scorecard_snapshot_job",
        "schedule": crontab(minute=20, hour=16, day_of_week="mon-fri"),
        "options": {"expires": 6 * 60 * 60},
    },
    # Warning-only operations health alerts every five minutes through the
    # regular session (the job checks the exact NYSE session itself and the
    # post-close runs resolve session-only alerts).
    "operations-health-alerts": {
        "task": "app.tasks.jobs.operations_health_alert_job",
        "schedule": crontab(minute="*/5", hour="9-16", day_of_week="mon-fri"),
        "options": {"queue": "risk", "expires": 4 * 60},
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


def excluded_schedule(schedule: dict, excluded: str) -> dict:
    """Drop comma-separated beat entry names so small hosts can shed research load."""
    names = {name.strip() for name in excluded.split(",") if name.strip()}
    return {name: entry for name, entry in schedule.items() if name not in names}


celery_app.conf.beat_schedule = excluded_schedule(
    celery_app.conf.beat_schedule, os.environ.get("PAPER_BEAT_EXCLUDE", ""),
)
# The task definitions live in `app.tasks.jobs`, rather than the conventional
# `app.tasks.tasks` module. Explicitly name that related module so standalone
# Celery workers register the same tasks as the API process.
celery_app.autodiscover_tasks(["app.tasks"], related_name="jobs")
