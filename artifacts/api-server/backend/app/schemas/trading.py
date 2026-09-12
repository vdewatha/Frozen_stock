from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class AssetCreate(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    name: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None


class AssetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    name: Optional[str]
    asset_type: str
    sector: Optional[str]
    industry: Optional[str]
    is_active: bool


class WatchlistDiscoveryResponse(BaseModel):
    generated_at: datetime
    source: str
    candidate_count: int
    candidates: list[dict]


class WatchlistImportRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    limit: int = Field(default=6, ge=1, le=10)
    period: str = "2y"
    import_prices: bool = True
    import_news: bool = True
    news_provider: str = Field(default="auto", pattern="^(auto|yfinance|nasdaq_rss|mock)$")
    refresh_candidates: bool = False


class WatchlistImportResponse(BaseModel):
    generated_at: datetime
    source: str
    activated_symbols: list[str]
    market_results: list[dict]
    news_results: list[dict]
    candidate_scan: Optional[dict] = None


class StrategyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    strategy_type: str
    description: Optional[str]
    parameters: dict
    is_active: bool
    current_status: str


class SignalRequest(BaseModel):
    symbol: str
    strategy: str


class MarketImportRequest(BaseModel):
    symbol: str = "SPY"
    period: str = "2y"

class IntradayImportRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)


class MarketImportResponse(BaseModel):
    symbol: str
    rows_imported: int
    start_date: Optional[date]
    end_date: Optional[date]
    source: str
    decision_journal: Optional[dict] = None
    memory_replay_gate_monitor: Optional[dict] = None


class PricePoint(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    adjusted_close: float
    volume: int
    source: str = "unknown"


class PriceHistoryResponse(BaseModel):
    symbol: str
    source: str
    rows: list[PricePoint]

class IntradayBarRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    symbol: str
    timeframe: str
    opened_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    provider: str
    feed_class: str
    exchange_timestamp: datetime
    ingested_at: datetime

class FeedStatusResponse(BaseModel):
    symbol: str
    status: str
    provider: Optional[str] = None
    feed_class: Optional[str] = None
    data_mode: Optional[str] = None
    timeframe: Optional[str] = None
    session: Optional[str] = None
    entitlement_configured: Optional[bool] = None
    entitlement_state: Optional[str] = None
    exchange_timestamp: Optional[datetime] = None
    ingestion_timestamp: Optional[datetime] = None
    latency_seconds: Optional[float] = None
    missing_intervals: list[str] = Field(default_factory=list)
    unavailable_reason: Optional[str] = None
    checked_at: Optional[datetime] = None
    adjustment_policy: Optional[str] = None


class SignalResponse(BaseModel):
    action: str
    probability_up: float
    probability_down: float
    confidence: float
    reason: str
    features: dict


class BacktestRequest(BaseModel):
    symbol: str = "SPY"
    strategy: str = "moving_average_crossover"
    starting_cash: float = 100_000
    risk_per_trade: float = 0.01
    fees_bps: float = 1
    slippage_bps: float = 5


class BacktestResponse(BaseModel):
    symbol: str
    strategy: str
    source: str
    total_return: float
    annualized_return: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    number_of_trades: int
    score: float
    rejected: bool
    rejection_reasons: list[str]
    equity_curve: list[dict]
    trades: list[dict]


class ModelPredictionRequest(BaseModel):
    symbol: str = "SPY"


class HorizonPrediction(BaseModel):
    horizon_days: int
    probability_up: float
    probability_down: float
    expected_return: float
    probabilities_by_model: dict


class WalkForwardFold(BaseModel):
    horizon_days: int
    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    sample_size: int
    accuracy: float
    brier_score: float


class ModelPredictionResponse(BaseModel):
    symbol: str
    source: str
    rows_used: int
    generated_at: datetime
    latest_features: dict
    macro_context: Optional[dict] = None
    predictions: list[HorizonPrediction]
    walk_forward: list[WalkForwardFold]
    warnings: list[str]


class PersistedModelPredictionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    prediction_date: date
    horizon_days: int
    probability_up: Decimal
    probability_down: Decimal
    expected_return: Decimal
    source: str
    realized_return: Optional[Decimal]
    realized_up: Optional[bool]
    brier_score: Optional[Decimal]
    is_realized: bool
    created_at: datetime


class PersistedModelRunResponse(ModelPredictionResponse):
    saved_prediction_ids: list[int]
    saved_validation_fold_ids: list[int]


class ModelRealizationScoreResponse(BaseModel):
    checked: int
    scored: int
    scored_prediction_ids: list[int]


class ModelPerformanceResponse(BaseModel):
    total_predictions: int
    realized_predictions: int
    avg_brier_score: Optional[float]
    hit_rate: Optional[float]
    predictions: list[PersistedModelPredictionRead]


class MarketRegimeRead(BaseModel):
    id: int
    regime_date: date
    spy_trend: Optional[str]
    volatility_regime: Optional[str]
    rate_regime: Optional[str]
    market_regime: Optional[str]
    features: dict


class MarketRegimeDetectionRequest(BaseModel):
    symbol: str = "SPY"


class NewsImportRequest(BaseModel):
    symbol: str = "SPY"
    provider: str = Field(default="auto", pattern="^(auto|yfinance|nasdaq_rss|mock)$")


class NewsImportResponse(BaseModel):
    symbol: str
    rows_imported: int
    article_ids: list[int]
    source: str
    error: Optional[str] = None


class NewsArticleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: Optional[str]
    published_at: Optional[datetime]
    source: Optional[str]
    title: Optional[str]
    url: Optional[str]
    summary: Optional[str]
    sentiment_score: Optional[Decimal]
    relevance_score: Optional[Decimal]
    raw_payload: Optional[dict]


class NewsSentimentSummary(BaseModel):
    symbol: str
    article_count: int
    average_sentiment: float
    sentiment_label: str
    summary: str
    top_headlines: list[dict]


class EconomicIndicatorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    indicator_name: str
    observation_date: date
    value: Optional[Decimal]
    unit: Optional[str]
    source: Optional[str]
    raw_payload: Optional[dict]
    created_at: datetime


class EconomicImportResponse(BaseModel):
    source: str
    rows_imported: int
    indicator_ids: list[int]


class MacroContextSummary(BaseModel):
    source: str
    indicator_count: int
    macro_label: str
    rate_regime: str
    inflation_regime: str
    labor_regime: str
    volatility_regime: str
    summary: str
    latest: dict


class PaperTradingSignalRequest(BaseModel):
    symbol: str = "SPY"
    strategy: str = "moving_average_crossover"


class BrokerOrderRequest(BaseModel):
    symbol: str = "SPY"
    side: str = "buy"
    quantity: float = Field(default=1, gt=0)
    order_type: str = "market"
    time_in_force: str = "day"


class BrokerOrderResponse(BaseModel):
    broker: str
    broker_order_id: Optional[str] = None
    symbol: Optional[str] = None
    side: Optional[str] = None
    quantity: Optional[float] = None
    order_type: Optional[str] = None
    time_in_force: Optional[str] = None
    status: str
    paper_only: bool
    submitted_at: Optional[str] = None
    source: Optional[str] = None
    paper_trade_id: Optional[int] = None
    signal_id: Optional[int] = None
    live_trading_enabled: Optional[bool] = None
    reason: Optional[str] = None


class BrokerStatusResponse(BaseModel):
    paper_broker: str
    paper_trading_enabled: bool
    live_trading_enabled: bool
    live_trading_blocked: bool
    message: str


class StockPaperHaltRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class StockPaperOrderRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    side: str = Field(pattern="^(buy|sell)$")
    quantity: Decimal = Field(gt=0)
    reference_price: Decimal = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=200)
    source: str = Field(default="manual_control_room", min_length=3, max_length=64)
    signal_id: Optional[int] = Field(default=None, gt=0)


class StockPaperReduceRequest(BaseModel):
    reduce_pct: Decimal = Field(gt=0, le=1)
    idempotency_key: str = Field(min_length=8, max_length=200)


class StockPaperCloseRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=200)


class StockPaperRecoveryRequest(BaseModel):
    flatten_policy: str = Field(default="none", pattern="^(none|positions)$")
    reason: str = Field(default="Operator-requested stock-paper recovery", min_length=3, max_length=500)


class StockPaperRollbackRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class PaperTradingRunResponse(BaseModel):
    symbol: str
    strategy: str
    action: str
    approved: bool
    reason: str
    signal_id: Optional[int]
    paper_trade_id: Optional[int]
    price: float
    quantity: float
    confidence: float
    broker_order: Optional[BrokerOrderResponse] = None


class PaperTradeReconcileResponse(BaseModel):
    checked: int
    closed: int
    closed_trade_ids: list[int]
    decision_journal: Optional[dict] = None
    memory_replay_gate_monitor: Optional[dict] = None


class PaperTradeCloseResponse(BaseModel):
    paper_trade_id: int
    status: str
    profit_loss: float
    profit_loss_pct: float
    reason: str


class PaperTradeReduceRequest(BaseModel):
    reduce_pct: float = Field(default=0.5, gt=0, le=1)
    reason: str = Field(default="Manual paper exposure reduction.", max_length=240)


class PaperTradeReduceResponse(BaseModel):
    paper_trade_id: int
    status: str
    reduced_quantity: float
    remaining_quantity: float
    realized_profit_loss: float
    realized_profit_loss_pct: float
    exit_price: float
    reason: str
    broker_order: Optional[BrokerOrderResponse] = None


class PaperTradeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    strategy_id: Optional[int]
    symbol: str
    side: str
    entry_time: Optional[datetime]
    exit_time: Optional[datetime]
    entry_price: Optional[Decimal]
    exit_price: Optional[Decimal]
    quantity: Optional[Decimal]
    status: Optional[str]
    profit_loss: Optional[Decimal]
    profit_loss_pct: Optional[Decimal]
    reason_entered: Optional[str]
    reason_exited: Optional[str]


class PortfolioRiskResponse(BaseModel):
    generated_at: datetime
    paper_equity: float
    open_positions: int
    gross_exposure: float
    total_notional: float
    total_unrealized_pl: float
    total_unrealized_pl_pct: float
    risk_limits: dict
    positions: list[dict]
    symbol_exposure: list[dict]
    strategy_exposure: list[dict]
    alerts: list[dict]
    data_sources: list[str]


class PortfolioAllocationResponse(BaseModel):
    generated_at: datetime
    status: str
    message: str
    paper_equity: float
    risk_limits: dict
    open_positions: int
    gross_exposure: float
    alerts: list[dict]
    memory_replay_gate: dict
    positive_candidates: int
    recommendations: list[dict]


class PortfolioAllocationExecuteRequest(BaseModel):
    dry_run: bool = True
    max_actions: int = Field(default=3, ge=1, le=10)
    limit: int = Field(default=20, ge=1, le=50)


class AllocationReviewRequest(BaseModel):
    notification_id: int
    symbol: str
    strategy: str
    decision: str = Field(pattern="^(approve|skip)$")
    reason: str = Field(default="Allocation review queue decision.", max_length=240)


class AllocationReviewDryRunRequest(BaseModel):
    symbol: str
    strategy: str
    journal_entry_id: Optional[int] = None
    limit: int = Field(default=20, ge=1, le=50)


class PortfolioAllocationExecuteResponse(BaseModel):
    generated_at: datetime
    status: str
    dry_run: bool
    message: str
    actions: list[dict]
    skipped: list[dict]
    readiness: dict
    plan: dict


class PortfolioRiskActionRequest(BaseModel):
    require_persistence: bool = True
    dry_run: bool = False


class PortfolioRiskActionResponse(BaseModel):
    status: str
    message: str
    breach_labels: list[str]
    previous_breach_labels: list[str]
    persistent_labels: list[str]
    actions_taken: list[dict]
    kill_switch_enabled: bool
    affected_strategy_ids: list[int]
    snapshot: PortfolioRiskResponse


class RiskRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    value: dict
    is_active: bool


class RiskSettingsUpdateRequest(BaseModel):
    min_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    max_daily_drawdown: Optional[float] = Field(default=None, ge=0, le=1)
    max_strategy_drawdown: Optional[float] = Field(default=None, ge=0, le=1)
    max_open_positions: Optional[int] = Field(default=None, ge=1, le=100)
    max_open_positions_per_strategy: Optional[int] = Field(default=None, ge=1, le=100)
    max_symbol_exposure: Optional[float] = Field(default=None, ge=0, le=1)
    max_risk_per_trade: Optional[float] = Field(default=None, ge=0, le=1)
    stop_after_consecutive_losses: Optional[int] = Field(default=None, ge=1, le=100)
    candidate_review_score_threshold: Optional[float] = Field(default=None, ge=0, le=2)
    activation_score_threshold: Optional[float] = Field(default=None, ge=0, le=2)
    journal_feedback_review_threshold_cap: Optional[float] = Field(default=None, ge=0, le=0.25)
    journal_feedback_allocation_multiplier_cap: Optional[float] = Field(default=None, ge=0, le=1)
    memory_replay_min_complete_samples: Optional[int] = Field(default=None, ge=0, le=1000)
    memory_replay_min_avg_return_delta: Optional[float] = Field(default=None, ge=-1, le=1)
    memory_replay_min_hit_rate_delta: Optional[float] = Field(default=None, ge=-1, le=1)
    reason: Optional[str] = "Risk settings updated from control room."


class SafetyControlRequest(BaseModel):
    reason: str = "Manual control-room action."


class SafetyControlResponse(BaseModel):
    action: str
    status: str
    message: str
    kill_switch_enabled: bool
    paused_strategies: int
    affected_strategy_ids: list[int]
    risk_rule: Optional[dict] = None


class StrategyMemoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    strategy_id: Optional[int]
    market_regime: Optional[str]
    symbol: Optional[str]
    sample_size: Optional[int]
    avg_return: Optional[Decimal]
    avg_drawdown: Optional[Decimal]
    win_rate: Optional[Decimal]
    profit_factor: Optional[Decimal]
    confidence_score: Optional[Decimal]
    last_updated: datetime
    notes: Optional[str]


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    entity_type: Optional[str]
    entity_id: Optional[int]
    action: str
    status: str
    message: Optional[str]
    payload: Optional[dict]
    created_at: datetime


class NotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    severity: str
    status: str
    source: str
    title: str
    message: Optional[str]
    entity_type: Optional[str]
    entity_id: Optional[int]
    payload: Optional[dict]
    created_at: datetime
    updated_at: datetime
    acknowledged_at: Optional[datetime]
    resolved_at: Optional[datetime]


class ReadinessCheck(BaseModel):
    name: str
    status: str
    message: str
    details: dict


class ReadinessResponse(BaseModel):
    generated_at: datetime
    overall_status: str
    summary: dict
    checks: list[ReadinessCheck]
    paper_trading_allowed: bool


class DeploymentMonitorResponse(BaseModel):
    generated_at: datetime
    environment: str
    deployable: bool
    status: str
    paper_trading_allowed: bool
    live_trading_allowed: bool
    message: str
    checks: list[ReadinessCheck]
    blockers: list[str]
    warnings: list[str]
    readiness_status: str
    readiness_blockers: list[str]
    readiness_warnings: list[str]
    readiness: dict


class TradeCandidate(BaseModel):
    symbol: str
    strategy: str
    strategy_name: str
    strategy_status: str
    strategy_research: dict
    candidate_status: str
    base_score: Optional[float] = None
    score: float
    memory_score_adjustment: float = 0.0
    review_threshold_adjustment: float = 0.0
    journal_feedback: dict = Field(default_factory=dict)
    action: str
    probability_up: float
    expected_return: float
    horizon_days: Optional[int]
    confidence: float
    backtest_score: float
    backtest_rejected: bool
    blockers: list[str]
    reason: str
    news_summary: str
    macro_summary: str
    market_regime: str
    data_source: str


class TradeCandidateResponse(BaseModel):
    generated_at: datetime
    cache_status: str
    cached_at: Optional[datetime] = None
    candidate_count: int
    positive_count: int
    candidates: list[TradeCandidate]


class TradeCandidateEvidenceResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    generated_at: datetime
    symbol: str
    strategy: str
    strategy_name: str
    strategy_status: str
    cached_candidate: dict
    market_data: dict
    model_evidence: dict
    signal_evidence: dict
    backtest_evidence: dict
    context_evidence: dict
    risk_room: dict


class ScannerRefreshJobRequest(BaseModel):
    trigger: str = Field(default="manual", max_length=64)
    limit: int = Field(default=50, ge=1, le=50)
    context: dict = Field(default_factory=dict)


class ScannerRefreshJobResponse(BaseModel):
    id: int
    trigger: str
    status: str
    message: Optional[str]
    payload: dict
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class CompanyOpportunityRadarResponse(BaseModel):
    generated_at: datetime
    asset_count: int
    scanner_cache_status: Optional[str] = None
    news_imports: list[dict] = Field(default_factory=list)
    opportunities: list[dict]


class TradeScorecardRow(BaseModel):
    paper_trade_id: int
    symbol: str
    strategy_name: str
    strategy_type: str
    status: str
    entry_time: Optional[datetime]
    exit_time: Optional[datetime]
    entry_price: float
    current_price: float
    quantity: float
    profit_loss: float
    profit_loss_pct: float
    age_days: int
    probability_up_at_entry: float
    expected_return_at_entry: float
    horizon_days: Optional[int]
    predictive_model_supported: bool
    on_track: bool
    reason_entered: Optional[str]
    reason_exited: Optional[str]


class TradeScorecardResponse(BaseModel):
    generated_at: datetime
    paper_trade_count: int
    open_trades: int
    closed_trades: int
    realized_win_rate: Optional[float]
    avg_realized_return: Optional[float]
    avg_open_return: Optional[float]
    positive_open_trades: int
    rows: list[TradeScorecardRow]


class StrategyGovernanceDecision(BaseModel):
    strategy_id: int
    strategy_name: str
    strategy_type: str
    old_status: str
    new_status: str
    decision: str
    reason: str
    memory: dict
    checks: dict
    thresholds: dict


class StrategyGovernanceResponse(BaseModel):
    evaluated: int
    changed: int
    thresholds: dict
    decisions: list[StrategyGovernanceDecision]
    comparison: Optional[dict] = None
    notifications: Optional[dict] = None


class StrategyGovernanceScorecardSnapshot(BaseModel):
    status: str
    message: Optional[str]
    scorecard: Optional[dict]
    audit_log_id: Optional[int]
    created_at: Optional[datetime]
    previous_audit_log_id: Optional[int]
    previous_created_at: Optional[datetime]
    comparison: dict


class StrategyReactivationCandidate(BaseModel):
    strategy_id: int
    strategy_name: str
    strategy_type: str
    current_status: str
    eligible: bool
    blockers: list[str]
    memory: dict


class StrategyReactivationQueueResponse(BaseModel):
    kill_switch_enabled: bool
    candidates: list[StrategyReactivationCandidate]


class StrategyImprovementQueueResponse(BaseModel):
    queued: int
    retired: int
    paused: int
    rows: list[dict]


class StrategyReactivationReviewRequest(BaseModel):
    strategy_id: int
    decision: str = Field(pattern="^(approve|reject|hold)$")
    reason: str = "Manual reactivation review from control room."


class StrategyReactivationReviewResponse(BaseModel):
    strategy_id: int
    strategy_name: str
    decision: str
    status: str
    message: str
    old_status: str
    new_status: str
    eligible: bool
    blockers: list[str]
    memory: dict


class CandidateActivationRequest(BaseModel):
    symbol: str
    strategy: str
    decision: str = Field(pattern="^(activate|candidate|reject)$")
    reason: str = "Positive scanner candidate review."


class CandidateActivationResponse(BaseModel):
    symbol: str
    strategy: str
    strategy_name: str
    decision: str
    status: str
    message: str
    old_status: str
    new_status: str
    eligible: bool
    blockers: list[str]
    candidate: dict
    review_context: dict = Field(default_factory=dict)
    journal_entry_id: Optional[int] = None


class CandidateDecisionJournalRequest(BaseModel):
    symbol: str
    strategy: str
    decision: str = Field(pattern="^(approve|reject|skip|review|activate|candidate)$")
    status: str = Field(default="reviewed", max_length=64)
    reason: str = "Manual candidate decision journal entry."


class CandidateDecisionJournalRead(BaseModel):
    id: int
    symbol: str
    strategy_id: Optional[int]
    strategy_type: str
    decision: str
    status: str
    reason: Optional[str]
    evidence_snapshot: dict
    paper_trade_id: Optional[int]
    realized_return: Optional[float]
    realized_status: Optional[str]
    created_at: datetime


class CandidateDecisionScorecardResponse(BaseModel):
    journal_count: int
    scored_count: int
    positive_outcomes: int
    hit_rate: Optional[float]
    avg_outcome_return: Optional[float]
    by_decision: list[dict]
    rows: list[dict]


class MemoryReplayResponse(BaseModel):
    generated_at: datetime
    top_k: int
    evaluated_rows: int
    complete_rows: int
    pending_rows: int
    risk_profile: dict
    baseline: dict
    memory_adjusted: dict
    delta: dict
    replay_gate: dict
    groups: dict
    approval_alerts: dict = Field(default_factory=dict)
    rows: list[dict]


class StrategyExperimentRunRequest(BaseModel):
    symbol: str = "SPY"
    strategy: str = "moving_average_crossover"
    max_candidates: int = Field(default=3, ge=1, le=10)
    apply_promotions: bool = False


class LearningWorkerScopeRequest(BaseModel):
    symbol: str = "SPY"
    strategy: str = "moving_average_crossover"
    max_candidates: int = Field(default=3, ge=1, le=10)


class LearningWorkerBatchRequest(BaseModel):
    limit_symbols: int = Field(default=8, ge=1, le=25)
    limit_strategies: int = Field(default=8, ge=1, le=25)
    max_candidates: int = Field(default=3, ge=1, le=10)


class LearningWorkerLaunchResponse(BaseModel):
    task_id: str
    status: str
    queue: str
    paper_only: bool
    message: str


class LearningWorkerStatusResponse(BaseModel):
    task_id: str
    status: str
    ready: bool
    successful: Optional[bool]
    result: Optional[Union[dict, list]]


class StrategyExperimentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    strategy_id: Optional[int]
    experiment_name: Optional[str]
    old_parameters: Optional[dict]
    new_parameters: Optional[dict]
    hypothesis: Optional[str]
    backtest_result_id: Optional[int]
    paper_result_summary: Optional[dict]
    decision: Optional[str]
    created_at: datetime


class StrategyExperimentRunResponse(BaseModel):
    symbol: str
    strategy: str
    source: str
    baseline_backtest_id: int
    baseline_score: float
    applied_parameters: Optional[dict]
    experiments: list[StrategyExperimentRead]


class DashboardSnapshot(BaseModel):
    paper_account_value: Optional[float]
    daily_pl: Optional[float]
    total_pl: Optional[float]
    metrics_status: str = "unavailable"
    performance_note: str
    active_strategies: int
    paused_strategies: int
    best_strategy: Optional[str]
    worst_strategy: Optional[str]
    open_paper_trades: int
    risk_state: str
    equity_curve: list[dict]
    strategies: list[dict]
    recent_trades: list[dict]
    experiments: list[dict]
    risk_rules: list[dict]
    generated_at: date
