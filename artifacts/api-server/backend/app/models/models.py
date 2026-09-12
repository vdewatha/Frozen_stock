from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    asset_type: Mapped[str] = mapped_column(String(32), default="stock")
    sector: Mapped[Optional[str]] = mapped_column(String(128))
    industry: Mapped[Optional[str]] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class MarketPrice(Base):
    __tablename__ = "market_prices"
    __table_args__ = (UniqueConstraint("symbol", "price_date", name="uq_market_prices_symbol_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    price_date: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    high: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    low: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    close: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    adjusted_close: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    volume: Mapped[Optional[int]] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(64), default="unknown")
    imported_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class IntradayBar(Base):
    __tablename__ = "intraday_bars"
    __table_args__ = (UniqueConstraint("symbol", "timeframe", "opened_at", name="uq_intraday_bar"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), default="1m")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    high: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    low: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    volume: Mapped[int] = mapped_column(BigInteger)
    provider: Mapped[str] = mapped_column(String(32), default="alpaca")
    feed_class: Mapped[str] = mapped_column(String(32), default="sip")
    exchange_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CorporateAction(Base):
    __tablename__ = "corporate_actions"
    __table_args__ = (UniqueConstraint("symbol", "action_type", "ex_date", "value", name="uq_corporate_action"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    action_type: Mapped[str] = mapped_column(String(32))
    ex_date: Mapped[date] = mapped_column(Date, index=True)
    value: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    provider: Mapped[str] = mapped_column(String(32), default="alpaca")
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSON)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NewsArticle(Base):
    __tablename__ = "news_articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[Optional[str]] = mapped_column(String(16), index=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    source: Mapped[Optional[str]] = mapped_column(String(128))
    title: Mapped[Optional[str]] = mapped_column(Text)
    url: Mapped[Optional[str]] = mapped_column(Text)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    sentiment_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    relevance_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSON)


class EconomicIndicator(Base):
    __tablename__ = "economic_indicators"
    __table_args__ = (UniqueConstraint("indicator_name", "observation_date", name="uq_economic_indicator_name_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    indicator_name: Mapped[str] = mapped_column(String(128), index=True)
    observation_date: Mapped[date] = mapped_column(Date, index=True)
    value: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    unit: Mapped[Optional[str]] = mapped_column(String(64))
    source: Mapped[Optional[str]] = mapped_column(String(128))
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MarketRegime(Base):
    __tablename__ = "market_regimes"

    id: Mapped[int] = mapped_column(primary_key=True)
    regime_date: Mapped[date] = mapped_column(Date, unique=True)
    spy_trend: Mapped[Optional[str]] = mapped_column(String(64))
    volatility_regime: Mapped[Optional[str]] = mapped_column(String(64))
    rate_regime: Mapped[Optional[str]] = mapped_column(String(64))
    market_regime: Mapped[Optional[str]] = mapped_column(String(64))
    features: Mapped[Optional[dict]] = mapped_column(JSON)


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    strategy_type: Mapped[str] = mapped_column(String(64))
    description: Mapped[Optional[str]] = mapped_column(Text)
    parameters: Mapped[dict] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    current_status: Mapped[str] = mapped_column(String(64), default="research")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    backtests: Mapped[list["StrategyBacktest"]] = relationship(back_populates="strategy")
    signals: Mapped[list["StrategySignal"]] = relationship(back_populates="strategy")
    paper_trades: Mapped[list["PaperTrade"]] = relationship(back_populates="strategy")


class StrategyBacktest(Base):
    __tablename__ = "strategy_backtests"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"))
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    train_start: Mapped[Optional[date]] = mapped_column(Date)
    train_end: Mapped[Optional[date]] = mapped_column(Date)
    test_start: Mapped[Optional[date]] = mapped_column(Date)
    test_end: Mapped[Optional[date]] = mapped_column(Date)
    parameters: Mapped[Optional[dict]] = mapped_column(JSON)
    total_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    annualized_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    sharpe_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    sortino_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    max_drawdown: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    win_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    profit_factor: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    number_of_trades: Mapped[Optional[int]]
    score: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    strategy: Mapped[Optional[Strategy]] = relationship(back_populates="backtests")


class StrategySignal(Base):
    __tablename__ = "strategy_signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"))
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    signal_time: Mapped[datetime] = mapped_column(DateTime)
    action: Mapped[str] = mapped_column(String(16))
    probability_up: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    probability_down: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    confidence: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    reason: Mapped[Optional[str]] = mapped_column(Text)
    features: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    strategy: Mapped[Optional[Strategy]] = relationship(back_populates="signals")


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"))
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(16))
    entry_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    exit_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    entry_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    exit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    status: Mapped[Optional[str]] = mapped_column(String(32))
    profit_loss: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    profit_loss_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    reason_entered: Mapped[Optional[str]] = mapped_column(Text)
    reason_exited: Mapped[Optional[str]] = mapped_column(Text)
    features_at_entry: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    strategy: Mapped[Optional[Strategy]] = relationship(back_populates="paper_trades")


class StrategyMemory(Base):
    __tablename__ = "strategy_memory"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"))
    market_regime: Mapped[Optional[str]] = mapped_column(String(64))
    symbol: Mapped[Optional[str]] = mapped_column(String(16))
    sample_size: Mapped[Optional[int]]
    avg_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    avg_drawdown: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    win_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    profit_factor: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    confidence_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    last_updated: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    notes: Mapped[Optional[str]] = mapped_column(Text)


class RiskRule(Base):
    __tablename__ = "risk_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    value: Mapped[dict] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class StrategyExperiment(Base):
    __tablename__ = "strategy_experiments"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"))
    experiment_name: Mapped[Optional[str]] = mapped_column(String(255))
    old_parameters: Mapped[Optional[dict]] = mapped_column(JSON)
    new_parameters: Mapped[Optional[dict]] = mapped_column(JSON)
    hypothesis: Mapped[Optional[str]] = mapped_column(Text)
    backtest_result_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategy_backtests.id"))
    paper_result_summary: Mapped[Optional[dict]] = mapped_column(JSON)
    decision: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CandidateDecisionJournal(Base):
    __tablename__ = "candidate_decision_journal"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"), index=True)
    strategy_type: Mapped[str] = mapped_column(String(64), index=True)
    decision: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    evidence_snapshot: Mapped[dict] = mapped_column(JSON)
    paper_trade_id: Mapped[Optional[int]] = mapped_column(ForeignKey("paper_trades.id"), index=True)
    realized_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    realized_status: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    entity_id: Mapped[Optional[int]] = mapped_column(index=True)
    action: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(64))
    message: Mapped[Optional[str]] = mapped_column(Text)
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    previous_event_sha256: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    event_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    source: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[Optional[str]] = mapped_column(Text)
    entity_type: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    entity_id: Mapped[Optional[int]] = mapped_column(index=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class StockMonitoringSnapshot(Base):
    """Append-only evidence from the continuous stock monitor."""
    __tablename__ = "stock_monitoring_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    checks: Mapped[list] = mapped_column(JSON, nullable=False)
    actions: Mapped[list] = mapped_column(JSON, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StockMonitoringBreach(Base):
    """Mutable state for persistence tracking; each evaluation is also snapshotted."""
    __tablename__ = "stock_monitoring_breaches"
    __table_args__ = (
        UniqueConstraint("breach_key", name="uq_stock_monitoring_breach_key"),
        CheckConstraint(
            "status IN ('unknown', 'observed', 'persistent', 'cleared')",
            name="ck_stock_monitoring_breach_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    breach_key: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(24), nullable=False)
    observed_value: Mapped[Optional[dict]] = mapped_column(JSON)
    threshold: Mapped[Optional[dict]] = mapped_column(JSON)
    consecutive_count: Mapped[int] = mapped_column(nullable=False, default=0)
    first_observed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StockPaperRecoveryState(Base):
    """Singleton coordination state for fail-closed paper recovery."""
    __tablename__ = "stock_paper_recovery_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_stock_paper_recovery_state_singleton"),
        CheckConstraint(
            "status IN ('armed', 'paused', 'cooldown', 'revalidation_required', 'resumable')",
            name="ck_stock_paper_recovery_status",
        ),
        CheckConstraint(
            "flatten_policy IN ('none', 'positions')",
            name="ck_stock_paper_recovery_flatten_policy",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="armed", index=True)
    flatten_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    cooldown_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    pause_reason: Mapped[Optional[str]] = mapped_column(Text)
    last_known_good_model_run_id: Mapped[Optional[str]] = mapped_column(String(64))
    last_known_good_binding_id: Mapped[Optional[int]] = mapped_column()
    last_monitor_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_watchdog_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_revalidation_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class StockPaperRecoveryEvent(Base):
    """Append-only evidence for cancellation, rollback, watchdog, and resume actions."""
    __tablename__ = "stock_paper_recovery_events"
    __table_args__ = (
        UniqueConstraint("event_sha256", name="uq_stock_paper_recovery_event_digest"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    event_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class ModelPrediction(Base):
    __tablename__ = "model_predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    prediction_date: Mapped[date] = mapped_column(Date, index=True)
    horizon_days: Mapped[int] = mapped_column(index=True)
    probability_up: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    probability_down: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    expected_return: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    source: Mapped[str] = mapped_column(String(64))
    features: Mapped[Optional[dict]] = mapped_column(JSON)
    probabilities_by_model: Mapped[Optional[dict]] = mapped_column(JSON)
    realized_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    realized_up: Mapped[Optional[bool]] = mapped_column(Boolean)
    brier_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    is_realized: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ModelValidationFold(Base):
    __tablename__ = "model_validation_folds"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    horizon_days: Mapped[int] = mapped_column(index=True)
    fold: Mapped[int]
    train_start: Mapped[date] = mapped_column(Date)
    train_end: Mapped[date] = mapped_column(Date)
    test_start: Mapped[date] = mapped_column(Date)
    test_end: Mapped[date] = mapped_column(Date)
    sample_size: Mapped[int]
    accuracy: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    brier_score: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class TradeCandidateSnapshot(Base):
    __tablename__ = "trade_candidate_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(64), default="scanner")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ScannerRefreshJob(Base):
    __tablename__ = "scanner_refresh_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(64), default="manual", index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    message: Mapped[Optional[str]] = mapped_column(Text)
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class ResearchModelRun(Base):
    __tablename__ = "research_model_runs"
    __table_args__ = (
        CheckConstraint("status = 'experimental'", name="ck_research_run_experimental"),
        CheckConstraint("eligible_for_trading = false", name="ck_research_run_ineligible"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="experimental")
    eligible_for_trading: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    training_metadata: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StockDatasetSnapshot(Base):
    __tablename__ = "stock_dataset_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # The bytes can legitimately be identical under distinct cutoff/horizon
    # snapshot identities; snapshot_id, not content hash, is immutable identity.
    dataset_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    cutoff_date: Mapped[date] = mapped_column(Date, nullable=False)
    universe: Mapped[list] = mapped_column(JSON, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_config_id: Mapped[str] = mapped_column(String(128), nullable=False)
    horizon_days: Mapped[int] = mapped_column(nullable=False)
    artifact_path: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StockHoldoutReservation(Base):
    __tablename__ = "stock_holdout_reservations"
    __table_args__ = (UniqueConstraint("snapshot_id", "horizon_days", name="uq_stock_holdout_snapshot_horizon"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    universe_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    universe: Mapped[list] = mapped_column(JSON, nullable=False)
    horizon_days: Mapped[int] = mapped_column(nullable=False, index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("stock_dataset_snapshots.snapshot_id"), nullable=False)
    reserved_by_job_id: Mapped[Optional[str]] = mapped_column(String(36), unique=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="model_selection")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StockHoldoutConsumption(Base):
    """Append-only, fenced proof that a reserved holdout may be scored once."""
    __tablename__ = "stock_holdout_consumptions"
    __table_args__ = (
        UniqueConstraint("reservation_id", name="uq_stock_holdout_consumption_reservation"),
        UniqueConstraint("claim_sha256", name="uq_stock_holdout_consumption_claim"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(
        ForeignKey("stock_holdout_reservations.id"), nullable=False
    )
    job_id: Mapped[str] = mapped_column(ForeignKey("stock_training_jobs.id"), nullable=False)
    attempt: Mapped[int] = mapped_column(nullable=False)
    claim_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StockModelRegistry(Base):
    __tablename__ = "stock_model_registry"
    __table_args__ = (
        CheckConstraint(
            "lifecycle_state IN ('challenger', 'eligible', 'paper_canary', 'champion', 'demoted', 'retired')",
            name="ck_stock_model_lifecycle_state",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("stock_dataset_snapshots.snapshot_id"), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    artifact_path: Mapped[str] = mapped_column(Text, nullable=False)
    training_metadata: Mapped[dict] = mapped_column(JSON, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String(24), nullable=False, default="challenger", server_default="challenger", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StockTrainingJob(Base):
    __tablename__ = "stock_training_jobs"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_stock_training_job_dedupe"),
        CheckConstraint("status IN ('queued', 'running', 'cancel_requested', 'cancelled', 'succeeded', 'failed', 'deferred')", name="ck_stock_training_job_status"),
        CheckConstraint("attempts >= 0 AND attempts <= 3", name="ck_stock_training_job_attempts"),
        CheckConstraint("delivery_attempts >= 0 AND delivery_attempts <= 3", name="ck_stock_training_job_deliveries"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    requested_by: Mapped[str] = mapped_column(String(32), nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("stock_dataset_snapshots.snapshot_id"), nullable=False)
    holdout_reservation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_holdout_reservations.id"))
    celery_task_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    delivery_attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    queue_error: Mapped[Optional[str]] = mapped_column(Text)
    failure_code: Mapped[Optional[str]] = mapped_column(String(96))
    failure_detail: Mapped[Optional[str]] = mapped_column(Text)
    result_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("stock_model_registry.run_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    recovery_attempted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

class StockLearningCycle(Base):
    """Mutable current pointer for one governed learning-cycle run.

    The linked datasets, models, holdout claims, trials, bindings, monitoring
    snapshots, and recovery events remain immutable or independently governed.
    This row only answers where the cycle is now; its history is in the event
    table below.
    """
    __tablename__ = "stock_learning_cycles"
    __table_args__ = (
        UniqueConstraint("request_sha256", name="uq_stock_learning_cycle_request"),
        CheckConstraint(
            "status IN ('blocked', 'deferred', 'queued', 'running', "
            "'awaiting_forward_evidence', 'operator_review', 'complete', "
            "'demoted', 'rolled_back', 'failed')",
            name="ck_stock_learning_cycle_status",
        ),
    )

    cycle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="blocked", index=True)
    stage: Mapped[str] = mapped_column(String(48), nullable=False, default="preflight", index=True)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    symbols: Mapped[list] = mapped_column(JSON, nullable=False)
    cutoff_date: Mapped[date] = mapped_column(Date, nullable=False)
    horizon_days: Mapped[int] = mapped_column(nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    seed: Mapped[int] = mapped_column(nullable=False, default=42)
    snapshot_id: Mapped[Optional[str]] = mapped_column(ForeignKey("stock_dataset_snapshots.snapshot_id"))
    training_job_id: Mapped[Optional[str]] = mapped_column(ForeignKey("stock_training_jobs.id"))
    model_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("stock_model_registry.run_id"))
    trial_id: Mapped[Optional[str]] = mapped_column(String(36))
    active_binding_id: Mapped[Optional[int]] = mapped_column()
    monitor_snapshot_id: Mapped[Optional[int]] = mapped_column()
    recovery_event_id: Mapped[Optional[int]] = mapped_column()
    gates: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
class StockPaperModelBinding(Base):
    __tablename__ = "stock_paper_model_bindings"
    __table_args__ = (
        CheckConstraint("paper_only = true", name="ck_stock_binding_paper_only"),
        CheckConstraint("live_authorized = false", name="ck_stock_binding_live_disabled"),
        UniqueConstraint("binding_sha256", name="uq_stock_binding_digest"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    model_run_id: Mapped[str] = mapped_column(ForeignKey("stock_model_registry.run_id"), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("stock_dataset_snapshots.snapshot_id"), nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    paper_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    live_authorized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    bound_by: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StockPaperBindingState(Base):
    """Singleton pointer to the one currently active paper binding.

    Binding rows remain immutable activation events. This row is the mutable
    coordination point used to enforce one active paper binding atomically.
    """
    __tablename__ = "stock_paper_binding_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_stock_paper_binding_state_singleton"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    active_binding_id: Mapped[int] = mapped_column(
        ForeignKey("stock_paper_model_bindings.id"), nullable=False, unique=True
    )
    changed_by: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StockModelLifecycleState(Base):
    """Mutable current lifecycle state for an otherwise immutable model row."""
    __tablename__ = "stock_model_lifecycle_state"
    __table_args__ = (
        CheckConstraint(
            "lifecycle_state IN ('challenger', 'eligible', 'paper_canary', 'champion', 'demoted', 'retired')",
            name="ck_stock_model_current_lifecycle_state",
        ),
        Index(
            "uq_stock_model_one_champion",
            "lifecycle_state",
            unique=True,
            sqlite_where=text("lifecycle_state = 'champion'"),
            postgresql_where=text("lifecycle_state = 'champion'"),
        ),
    )

    model_run_id: Mapped[str] = mapped_column(
        ForeignKey("stock_model_registry.run_id"), primary_key=True
    )
    lifecycle_state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StockModelLifecycleEvent(Base):
    """Append-only evidence for every explicit model lifecycle transition."""
    __tablename__ = "stock_model_lifecycle_events"
    __table_args__ = (
        CheckConstraint(
            "to_state IN ('challenger', 'eligible', 'paper_canary', 'champion', 'demoted', 'retired')",
            name="ck_stock_model_lifecycle_event_to_state",
        ),
        CheckConstraint(
            "from_state IS NULL OR from_state IN ('challenger', 'eligible', 'paper_canary', 'champion', 'demoted', 'retired')",
            name="ck_stock_model_lifecycle_event_from_state",
        ),
        UniqueConstraint("event_sha256", name="uq_stock_model_lifecycle_event_digest"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    model_run_id: Mapped[str] = mapped_column(ForeignKey("stock_model_registry.run_id"), nullable=False, index=True)
    binding_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_paper_model_bindings.id"), index=True)
    from_state: Mapped[Optional[str]] = mapped_column(String(24))
    to_state: Mapped[str] = mapped_column(String(24), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    event_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StockPaperTrial(Base):
    """Immutable, separately governed controlled forward-paper trial."""
    __tablename__ = "stock_paper_trials"
    __table_args__ = (
        CheckConstraint("status IN ('approved','blocked','paused','running','stopped','completed')", name="ck_stock_trial_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    binding_id: Mapped[int] = mapped_column(ForeignKey("stock_paper_model_bindings.id"), nullable=False)
    strategy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("strategies.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="approved", index=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    policy: Mapped[dict] = mapped_column(JSON, nullable=False)
    lineage: Mapped[dict] = mapped_column(JSON, nullable=False)
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text)
    pause_reason: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    baseline_equity: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    baseline_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    peak_equity: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    stopped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StockPaperTrialDecision(Base):
    """Deduplicated completed-bar decision, including rejected decisions."""
    __tablename__ = "stock_paper_trial_decisions"
    __table_args__ = (
        UniqueConstraint("trial_id", "symbol", "bar_timestamp", name="uq_stock_trial_decision_bar"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    trial_id: Mapped[str] = mapped_column(ForeignKey("stock_paper_trials.id"), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    bar_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)
    qualifying: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lineage: Mapped[dict] = mapped_column(JSON, nullable=False)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_paper_orders.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StockPaperTrialMetric(Base):
    __tablename__ = "stock_paper_trial_metrics"
    __table_args__ = (UniqueConstraint("trial_id", "as_of", name="uq_stock_trial_metric_asof"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    trial_id: Mapped[str] = mapped_column(ForeignKey("stock_paper_trials.id"), nullable=False, index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    classification: Mapped[str] = mapped_column(String(24), nullable=False, default="accumulating")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class StockPaperPromotionReadinessReport(Base):
    """Append-only, paper-only readiness evidence; never grants promotion."""
    __tablename__ = "stock_paper_promotion_readiness_reports"
    __table_args__ = (
        CheckConstraint("paper_only = true", name="ck_stock_readiness_paper_only"),
        CheckConstraint("live_authorized = false", name="ck_stock_readiness_live_disabled"),
        UniqueConstraint("report_hash", name="uq_stock_readiness_report_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    trial_id: Mapped[str] = mapped_column(ForeignKey("stock_paper_trials.id"), nullable=False, index=True)
    source_metric_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_paper_trial_metrics.id"), index=True)
    report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    gates: Mapped[dict] = mapped_column(JSON, nullable=False)
    lineage: Mapped[dict] = mapped_column(JSON, nullable=False)
    policy: Mapped[dict] = mapped_column(JSON, nullable=False)
    paper_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    live_authorized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

class StockLearningCycleEvent(Base):
    """Append-only stage and gate evidence for a learning-cycle run."""
    __tablename__ = "stock_learning_cycle_events"
    __table_args__ = (
        UniqueConstraint("decision_sha256", name="uq_stock_learning_cycle_event_digest"),
        CheckConstraint(
            "decision IN ('pass', 'fail', 'unknown', 'blocked', 'deferred', 'complete')",
            name="ck_stock_learning_cycle_event_decision",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[str] = mapped_column(ForeignKey("stock_learning_cycles.cycle_id"), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decision_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
