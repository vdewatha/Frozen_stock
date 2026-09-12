from __future__ import annotations

from celery import Celery
from kombu import Exchange, Queue

from app.core.config import settings

celery_app = Celery("trading_app", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.timezone = "America/New_York"
celery_app.conf.task_queues = (
    Queue("default", Exchange("default"), routing_key="default"),
    Queue("market_data", Exchange("market_data"), routing_key="market_data"),
    Queue("learning", Exchange("learning"), routing_key="learning"),
    Queue("paper_trading", Exchange("paper_trading"), routing_key="paper_trading"),
    Queue("risk", Exchange("risk"), routing_key="risk"),
)
celery_app.conf.task_default_queue = "default"
celery_app.conf.task_routes = {
    "app.tasks.jobs.daily_market_data_import": {"queue": "market_data"},
    "app.tasks.jobs.intraday_market_data_import": {"queue": "market_data"},
    "app.tasks.jobs.daily_economic_data_import": {"queue": "market_data"},
    "app.tasks.jobs.daily_news_import": {"queue": "market_data"},
    "app.tasks.jobs.daily_feature_generation": {"queue": "market_data"},
    "app.tasks.jobs.daily_market_regime_detection": {"queue": "market_data"},
    "app.tasks.jobs.trade_candidate_scan_job": {"queue": "market_data"},
    "app.tasks.jobs.nightly_backtest_job": {"queue": "learning"},
    "app.tasks.jobs.nightly_strategy_learning_job": {"queue": "learning"},
    "app.tasks.jobs.strategy_learning_scope_job": {"queue": "learning"},
    "app.tasks.jobs.strategy_learning_batch_job": {"queue": "learning"},
    "app.tasks.jobs.paper_trading_signal_job": {"queue": "paper_trading"},
    "app.tasks.jobs.paper_trade_reconciliation_job": {"queue": "paper_trading"},
    "app.tasks.jobs.memory_replay_gate_monitor_job": {"queue": "learning"},
    "app.tasks.jobs.deployment_monitor_job": {"queue": "risk"},
    "app.tasks.jobs.strategy_promotion_job": {"queue": "risk"},
    "app.tasks.jobs.risk_monitor_job": {"queue": "risk"},
    "app.tasks.jobs.stock_monitoring_job": {"queue": "risk"},
    "app.tasks.jobs.stock_training_job": {"queue": "learning"},
    "app.tasks.jobs.recover_stock_training_jobs_job": {"queue": "learning"},
    "app.tasks.jobs.scheduled_stock_challenger_retraining_job": {"queue": "learning"},
    "app.tasks.jobs.stock_forward_trial_observe_job": {"queue": "paper_trading"},
    "app.tasks.jobs.stock_forward_trial_reconcile_job": {"queue": "paper_trading"},
    "app.tasks.jobs.stock_forward_trial_evaluate_job": {"queue": "paper_trading"},
}
celery_app.conf.beat_schedule = {
    "daily-market-data-import": {"task": "app.tasks.jobs.daily_market_data_import", "schedule": 60 * 60 * 24},
    "intraday-market-data-import": {"task": "app.tasks.jobs.intraday_market_data_import", "schedule": 60},
    "daily-economic-data-import": {"task": "app.tasks.jobs.daily_economic_data_import", "schedule": 60 * 60 * 24},
    "daily-news-import": {"task": "app.tasks.jobs.daily_news_import", "schedule": 60 * 60 * 24},
    "daily-feature-generation": {"task": "app.tasks.jobs.daily_feature_generation", "schedule": 60 * 60 * 24},
    "trade-candidate-scan-job": {"task": "app.tasks.jobs.trade_candidate_scan_job", "schedule": 60 * 60 * 6},
    "daily-market-regime-detection": {"task": "app.tasks.jobs.daily_market_regime_detection", "schedule": 60 * 60 * 24},
    "nightly-backtest-job": {"task": "app.tasks.jobs.nightly_backtest_job", "schedule": 60 * 60 * 24},
    "nightly-learning-job": {"task": "app.tasks.jobs.nightly_strategy_learning_job", "schedule": 60 * 60 * 24},
    "strategy-learning-batch-job": {"task": "app.tasks.jobs.strategy_learning_batch_job", "schedule": 60 * 60 * 6},
    "paper-trading-signal-job": {"task": "app.tasks.jobs.paper_trading_signal_job", "schedule": 60 * 60},
    "paper-trade-reconciliation-job": {"task": "app.tasks.jobs.paper_trade_reconciliation_job", "schedule": 60 * 30},
    "memory-replay-gate-monitor-job": {"task": "app.tasks.jobs.memory_replay_gate_monitor_job", "schedule": 60 * 30},
    "deployment-monitor-job": {"task": "app.tasks.jobs.deployment_monitor_job", "schedule": 60 * 15},
    "strategy-promotion-job": {"task": "app.tasks.jobs.strategy_promotion_job", "schedule": 60 * 60 * 24},
    "risk-monitor-job": {"task": "app.tasks.jobs.risk_monitor_job", "schedule": 60 * 5},
    "stock-monitoring-job": {"task": "app.tasks.jobs.stock_monitoring_job", "schedule": 60 * 5},
    "stock-training-recovery-job": {"task": "app.tasks.jobs.recover_stock_training_jobs_job", "schedule": 60 * 5},
    "scheduled-stock-challenger-retraining": {"task": "app.tasks.jobs.scheduled_stock_challenger_retraining_job", "schedule": 60 * 60 * 24},
    "stock-forward-trial-reconcile": {"task": "app.tasks.jobs.stock_forward_trial_reconcile_job", "schedule": 60},
    "stock-forward-trial-observe": {"task": "app.tasks.jobs.stock_forward_trial_observe_job", "schedule": 60},
}

celery_app.autodiscover_tasks(["app.tasks.jobs"])
