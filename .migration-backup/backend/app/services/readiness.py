from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Asset, MarketPrice, ModelPrediction, Notification, RiskRule, Strategy
from app.services.broker import broker_status
from app.services.portfolio_risk import portfolio_risk_snapshot
from app.tasks.celery_app import celery_app
from app.services.trusted_data import trusted_history, UntrustedMarketData, TRUSTED_SOURCES


def _check(name: str, status: str, message: str, details: Optional[dict] = None) -> dict:
    return {"name": name, "status": status, "message": message, "details": details or {}}


def _status_from_count(failing: int, warning: int = 0) -> str:
    if failing:
        return "blocked"
    if warning:
        return "warning"
    return "ready"


def readiness_snapshot(db: Session) -> dict:
    checks: list[dict] = []
    today = date.today()

    active_assets = db.query(Asset).filter(Asset.is_active.is_(True)).order_by(Asset.symbol).all()
    stale_assets: list[str] = []
    missing_assets: list[str] = []
    untrusted_assets: list[str] = []
    latest_prices: dict[str, str] = {}
    latest_sources: dict[str, str] = {}
    trusted_sources = TRUSTED_SOURCES
    invalid_histories: dict[str, str] = {}
    for asset in active_assets:
        try:
            trusted_history(db, asset.symbol, require_active=False)
        except UntrustedMarketData as exc:
            invalid_histories[asset.symbol] = str(exc)
        latest_price = (
            db.query(MarketPrice)
            .filter(MarketPrice.symbol == asset.symbol)
            .order_by(MarketPrice.price_date.desc())
            .first()
        )
        if not latest_price:
            missing_assets.append(asset.symbol)
            continue
        latest_price_date = latest_price.price_date
        latest_prices[asset.symbol] = str(latest_price_date)
        latest_sources[asset.symbol] = latest_price.source or "unknown"
        if (today - latest_price_date).days > 7:
            stale_assets.append(asset.symbol)
        if (latest_price.source or "unknown") not in trusted_sources:
            untrusted_assets.append(asset.symbol)
    checks.append(
        _check(
            "Market data",
            _status_from_count(len(invalid_histories) + int(not active_assets), len(stale_assets)),
            "Active assets have recent trusted market data."
            if not missing_assets and not stale_assets and not untrusted_assets
            else "Market data is missing, stale, or not from a trusted provider for one or more active assets.",
            {
                "active_assets": len(active_assets),
                "invalid_histories": invalid_histories,
                "missing_assets": missing_assets,
                "stale_assets": stale_assets,
                "untrusted_assets": untrusted_assets,
                "latest_prices": latest_prices,
                "latest_sources": latest_sources,
                "trusted_sources": sorted(trusted_sources),
            },
        )
    )

    latest_model = db.query(func.max(ModelPrediction.created_at)).filter(
        ModelPrediction.source.in_(list(TRUSTED_SOURCES) + [f"database:{source}" for source in TRUSTED_SOURCES])
    ).scalar()
    model_age_hours = ((datetime.utcnow() - latest_model).total_seconds() / 3600) if latest_model else None
    checks.append(
        _check(
            "Model freshness",
            "ready" if model_age_hours is not None and model_age_hours <= 48 else "warning",
            "Stored model predictions are fresh." if model_age_hours is not None and model_age_hours <= 48 else "No recent stored model predictions.",
            {"latest_model_run": latest_model.isoformat() if latest_model else None, "age_hours": round(model_age_hours, 2) if model_age_hours is not None else None},
        )
    )

    broker = broker_status()
    checks.append(
        _check(
            "Broker safety",
            "ready" if broker["paper_trading_enabled"] and broker["live_trading_blocked"] else "blocked",
            broker["message"],
            broker,
        )
    )

    risk_rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    rule_value = (risk_rule.value if risk_rule else {}) or {}
    portfolio_risk = portfolio_risk_snapshot(db)
    breach_alerts = [alert for alert in portfolio_risk["alerts"] if alert.get("severity") == "breach"]
    kill_switch_enabled = bool(rule_value.get("kill_switch_enabled", False))
    checks.append(
        _check(
            "Risk state",
            "blocked" if risk_rule is None or kill_switch_enabled or breach_alerts else "ready",
            "Risk state permits paper signals." if risk_rule is not None and not kill_switch_enabled and not breach_alerts else "Risk state blocks new paper signals.",
            {"risk_rule_present": risk_rule is not None, "kill_switch_enabled": kill_switch_enabled, "breach_alerts": breach_alerts, "paper_only": bool(rule_value.get("paper_only", True))},
        )
    )

    active_strategies = db.query(Strategy).filter(Strategy.current_status == "paper_trading_active").count()
    candidate_strategies = db.query(Strategy).filter(Strategy.current_status == "paper_trading_candidate").count()
    checks.append(
        _check(
            "Strategy availability",
            "ready" if active_strategies > 0 else "warning",
            "At least one strategy is active for paper trading." if active_strategies > 0 else "No strategy is currently active for paper trading.",
            {"active_strategies": active_strategies, "candidate_strategies": candidate_strategies},
        )
    )

    open_critical_notifications = (
        db.query(Notification)
        .filter(Notification.status != "resolved", Notification.severity == "critical")
        .count()
    )
    checks.append(
        _check(
            "Critical notifications",
            "blocked" if open_critical_notifications else "ready",
            "No unresolved critical notifications." if not open_critical_notifications else "Critical notifications need review.",
            {"unresolved_critical": open_critical_notifications},
        )
    )

    job_names = sorted(celery_app.conf.beat_schedule.keys())
    recent_job_failures = (
        db.query(Notification)
        .filter(
            Notification.category == "scheduled_job",
            Notification.status != "resolved",
            Notification.created_at >= datetime.utcnow() - timedelta(days=1),
        )
        .count()
    )
    checks.append(
        _check(
            "Scheduler health",
            "blocked" if recent_job_failures else "ready",
            "Scheduled jobs are configured with no unresolved recent failures." if not recent_job_failures else "Recent scheduled job failures are unresolved.",
            {"configured_jobs": job_names, "recent_unresolved_failures": recent_job_failures},
        )
    )

    summary = {
        "ready": sum(1 for check in checks if check["status"] == "ready"),
        "warning": sum(1 for check in checks if check["status"] == "warning"),
        "blocked": sum(1 for check in checks if check["status"] == "blocked"),
    }
    overall_status = "blocked" if summary["blocked"] else ("warning" if summary["warning"] else "ready")

    return {
        "generated_at": datetime.utcnow(),
        "overall_status": overall_status,
        "summary": summary,
        "checks": checks,
        "paper_trading_allowed": overall_status == "ready",
    }
