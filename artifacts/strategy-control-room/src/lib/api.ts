export type DashboardSnapshot = {
  paper_account_value: number | null;
  daily_pl: number | null;
  total_pl: number | null;
  metrics_status: string;
  performance_note: string;
  active_strategies: number;
  paused_strategies: number;
  best_strategy: string | null;
  worst_strategy: string | null;
  open_paper_trades: number;
  risk_state: string;
  equity_curve: { date: string; value?: number; equity?: number }[];
  strategies: {
    id: number;
    name: string;
    status: string;
    score: number | null;
    win_rate: number | null;
    drawdown: number | null;
    profit_factor: number | null;
    last_updated: string | null;
  }[];
  recent_trades: {
    id?: number;
    symbol: string;
    side: string;
    status: string;
    profit_loss: number | null;
    confidence: number | null;
    reason: string;
  }[];
  experiments: {
    experiment_name: string;
    old_parameters: Record<string, unknown>;
    new_parameters: Record<string, unknown>;
    hypothesis: string;
    decision: string;
  }[];
  risk_rules: RiskRule[];
  generated_at: string;
};

export type AccessRole = "viewer" | "researcher" | "operator" | "admin";
export type AuthSession = {
  role: AccessRole;
  identity?: string;
  permissions?: AccessRole[];
  auth_method?: string;
  environment?: string;
  live_mode?: string;
  live_orders_allowed?: boolean;
};
export type AuthConfig = {
  mode: "local_role_keys" | "production_identity" | string;
  requires_identity_provider: boolean;
  paper_only: boolean;
  live_orders_allowed: boolean;
};

export type OperationalHardeningReport = {
  status: "clear" | "unknown" | "breach" | string;
  generated_at: string;
  source: string;
  checks: Array<{
    key: string;
    status: "clear" | "unknown" | "breach" | string;
    message: string;
    details: Record<string, unknown>;
  }>;
  configuration_digest: {
    sha256: string;
    inputs: Record<string, unknown>;
  };
  incident_procedure: string;
};

export type LiveOperationsStatus = "healthy" | "degraded" | "uncertain" | "blocked" | "unknown" | string;

export type LiveOperationsComponent = {
  status: LiveOperationsStatus;
  reason: string;
  observed_at: string | null;
  details: Record<string, unknown>;
};

export type LiveOperationsSnapshot = {
  generated_at: string;
  status: LiveOperationsStatus;
  mode: string | null;
  live_orders_allowed: boolean;
  components: Record<string, LiveOperationsComponent>;
  metrics: {
    status: LiveOperationsStatus;
    reason: string;
    observed_at: string | null;
    orders_last_24h: number;
    rejected_orders_last_24h: number;
    uncertain_orders: number;
    open_orders: number;
    average_submission_latency_seconds: number | null;
    max_submission_latency_seconds: number | null;
    repeated_retry_events_last_24h: number;
  };
  account: {
    broker: string;
    account_id: string;
    environment: string;
    currency: string;
    cash: string;
    buying_power: string;
    equity: string;
    status: string;
    reconciliation_required: boolean;
    unexplained_residual: boolean;
    accounting_review_required: boolean;
    last_reconciled_at: string | null;
  } | null;
  open_orders: Array<{
    id: number;
    client_order_id: string;
    broker_order_id: string | null;
    symbol: string;
    side: string;
    quantity: string;
    status: string;
    model_run_id: string | null;
    signal_id: number | null;
    risk_decision_id: string;
    actor: string;
    request_id: string | null;
    uncertain_submission: boolean;
    created_at: string | null;
    submitted_at: string | null;
  }>;
  positions: Array<{
    symbol: string;
    quantity: string;
    current_price: string | null;
    market_value: string | null;
    observed_at: string | null;
  }>;
  recent_fills: Array<{
    broker_activity_id: string;
    broker_order_id: string | null;
    symbol: string;
    side: string;
    quantity: string;
    price: string;
    fee_known: boolean;
    filled_at: string | null;
  }>;
  alerts: Array<{
    id: number | null;
    state: LiveOperationsStatus;
    severity: string;
    title: string;
    reason: string;
    observed_at: string;
    acknowledged: boolean;
    source: string;
  }>;
  safety: {
    status: string;
    last_reason: string;
    updated_at: string | null;
    gates: Record<string, { status: string; reason?: string }>;
  };
};

export type LivePilot = {
  id: number;
  status: string;
  active: boolean;
  symbols: string[];
  max_notional: string;
  max_order_notional: string;
  allowed_order_types: string[];
  time_in_force: string;
  session_policy: string;
  starts_at: string | null;
  expires_at: string | null;
  observation_window_sessions: number;
  rollback_target: string;
  model_run_id: string | null;
  paper_expectations: Record<string, unknown>;
  launch_checklist: Record<string, unknown>;
  primary_approval_actor: string | null;
  secondary_approval_actor: string | null;
  approved_at: string | null;
  stopped_at: string | null;
  stopped_reason: string | null;
  latest_review: Record<string, unknown> | null;
  updated_by: string;
  updated_at: string | null;
  events: Array<{
    id: number;
    action: string;
    status: string;
    actor: string;
    secondary_actor: string | null;
    reason: string;
    created_at: string | null;
  }>;
};

export type StockPaperStatusValue = "uninitialized" | "reconciled" | "halted" | "drift" | "uncertain" | "unavailable";

export type StockPaperAccount = {
  broker: "alpaca_paper" | string;
  account_id: string;
  currency: string;
  cash: string | null;
  buying_power: string | null;
  equity: string | null;
  last_equity: string | null;
  initialized_at: string;
  last_reconciled_at: string | null;
  source_timestamp: string | null;
  reconciliation_required: boolean;
  halt_reason: string | null;
};

export type StockPaperPosition = {
  symbol: string;
  quantity: string | null;
  average_entry_price: string | null;
  current_price: string | null;
  market_value: string | null;
  cost_basis: string | null;
  unrealized_pl: string | null;
  observed_at: string;
};

export type StockPaperOrder = {
  id: number;
  client_order_id: string;
  broker_order_id: string | null;
  symbol: string;
  side: string;
  quantity: string | null;
  order_type?: string;
  limit_price?: string | null;
  signal_id?: number | null;
  strategy_id?: number | null;
  evidence_id?: string | null;
  status: string;
  reserved_cash: string | null;
  uncertain_submission: boolean;
  submitted_at?: string | null;
};

export type StockPaperOrderActionResponse = {
  mode: "paper";
  action: "reserved" | "submitted" | "halted_uncertain" | "reserved_close" | "reserved_reduce" | string;
  order: StockPaperOrder;
};

export type StockPaperFill = {
  broker_activity_id: string;
  broker_order_id: string | null;
  symbol: string;
  side: string;
  quantity: string | null;
  price: string | null;
  fee: string | null;
  cost_known: boolean;
  filled_at: string;
};

export type StockPaperEquitySnapshot = {
  cash: string | null;
  equity: string | null;
  last_equity: string | null;
  buying_power: string | null;
  observed_at: string;
};

export type StockPaperStatus = {
  status: StockPaperStatusValue;
  reason: string;
  mode: "paper";
  broker?: string;
  broker_evidence?: {
    provider: string;
    complete: boolean | null;
    status: string;
    orders_scope?: string;
    transaction_history_scope?: string;
    costs_scope?: string;
    restart_recovery?: string;
  };
  legacy_nonqualifying: true;
  costs_known: boolean;
  account: StockPaperAccount | null;
  positions: StockPaperPosition[];
  orders: StockPaperOrder[];
  fills: StockPaperFill[];
  equity_snapshots: StockPaperEquitySnapshot[];
};

export type StockPaperRecoveryStatus = {
  status: "armed" | "paused" | "cooldown" | "revalidation_required" | "resumable" | string;
  flatten_policy: "none" | "positions" | string;
  cooldown_until: string | null;
  pause_reason: string | null;
  last_known_good_model_run_id: string | null;
  last_known_good_binding_id: number | null;
  last_monitor_heartbeat_at: string | null;
  last_watchdog_heartbeat_at: string | null;
  last_revalidation_at: string | null;
  accounting_review_required: boolean;
  accounting_reviewed_at: string | null;
  accounting_reviewed_by: string | null;
  accounting_review_reason: string | null;
  automatic_review_enabled: boolean;
  automatic_review_status: "blocked" | "complete" | "not_required" | string;
  account_status: string;
  accounting_residual: boolean;
  account_reconciliation_required: boolean;
  account_halt_reason: string | null;
  accounting_verified: boolean;
  costs_known: boolean;
  last_monitoring_preflight: {
    status: "blocked" | "clear" | string;
    actor: string;
    reason: string;
    created_at: string | null;
    payload: { checked_at?: string } & Record<string, unknown>;
  } | null;
  events: Array<{ id: number; action: string; status: string; actor: string; reason: string; created_at: string | null; payload: Record<string, unknown> }>;
};

export type PricePoint = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  adjusted_close: number;
  volume: number;
  source: string;
};

export type PriceHistoryResponse = {
  symbol: string;
  source: string;
  rows: PricePoint[];
};

export type MarketImportResponse = {
  symbol: string;
  rows_imported: number;
  start_date: string | null;
  end_date: string | null;
  source: string;
};

export type IntradayImportResponse = {
  provider: string;
  feed_class: string;
  data_mode?: string;
  cadence: string;
  session?: string;
  adjustment_policy?: string;
  results: Array<{
    symbol: string;
    status: string;
    rows_imported?: number;
    unavailable_reason?: string;
    missing_intervals?: string[];
    deferred_window?: { start: string; end: string } | null;
    oldest_unresolved_interval?: string | null;
  }>;
};

export type IntradayPreflightResponse = {
  provider: string;
  feed_class: string;
  data_mode: string;
  cadence: string;
  session: string;
  adjustment_policy: string;
  checked_at: string;
  symbols: string[];
  status: string;
  ready: boolean;
  failure_class: string | null;
  reason: string | null;
  next_regular_session_open: string | null;
  next_regular_session_gap: "weekend" | "NYSE holiday" | "weekend and NYSE holiday" | null;
  results: Array<{
    symbol: string;
    status: string;
    failure_class?: string | null;
    entitlement_state?: string;
    exchange_timestamp?: string | null;
    ingestion_timestamp?: string | null;
    latency_seconds?: number | null;
    missing_intervals: string[];
    deferred_window?: { start: string; end: string } | null;
    oldest_unresolved_interval?: string | null;
    rows_imported?: number;
    unavailable_reason?: string | null;
  }>;
};
export type MarketDataHealth = {
  symbol: string;
  provider: string;
  feed?: string;
  feed_class?: string;
  entitlement_configured?: boolean;
  entitlement_state?: string;
  exchange_timestamp: string | null;
  ingestion_time?: string | null;
  ingestion_timestamp?: string | null;
  latency_seconds: number | null;
  missing_intervals: string[];
  deferred_window?: { start: string; end: string } | null;
  oldest_unresolved_interval?: string | null;
  is_stale?: boolean;
  is_incomplete?: boolean;
  status: string;
  unavailable_reason: string | null;
  data_mode?: "historical" | "delayed" | "real-time" | string;
  timeframe?: string;
  session?: string;
  checked_at?: string;
};

export type BacktestResponse = {
  symbol: string;
  strategy: string;
  source: string;
  total_return: number;
  annualized_return: number;
  sharpe_ratio: number;
  sortino_ratio: number;
  max_drawdown: number;
  win_rate: number;
  profit_factor: number;
  number_of_trades: number;
  score: number;
  rejected: boolean;
  rejection_reasons: string[];
  equity_curve: { date: string; value: number }[];
  trades: {
    entry_date: string;
    exit_date: string;
    entry_price: number;
    exit_price: number;
    quantity: number;
    profit_loss: number;
    profit_loss_pct: number;
  }[];
};

export type ModelPredictionResponse = {
  symbol: string;
  source: string;
  rows_used: number;
  generated_at: string;
  latest_features: Record<string, number>;
  predictions: {
    horizon_days: number;
    probability_up: number;
    probability_down: number;
    expected_return: number;
    probabilities_by_model: Record<string, number>;
  }[];
  walk_forward: {
    horizon_days: number;
    fold: number;
    train_start: string;
    train_end: string;
    test_start: string;
    test_end: string;
    sample_size: number;
    accuracy: number;
    brier_score: number;
  }[];
  warnings: string[];
};

export type PaperTrade = {
  id: number;
  strategy_id: number | null;
  symbol: string;
  side: string;
  entry_time: string | null;
  exit_time: string | null;
  entry_price: string | number | null;
  exit_price: string | number | null;
  quantity: string | number | null;
  status: string | null;
  profit_loss: string | number | null;
  profit_loss_pct: string | number | null;
  reason_entered: string | null;
  reason_exited: string | null;
};

export type PortfolioRiskSnapshot = {
  generated_at: string;
  paper_equity: number;
  open_positions: number;
  gross_exposure: number;
  total_notional: number;
  total_unrealized_pl: number;
  total_unrealized_pl_pct: number;
  risk_limits: {
    max_open_positions: number;
    max_open_positions_per_strategy: number;
    max_symbol_exposure: number;
    max_daily_drawdown: number;
    kill_switch_enabled: boolean;
    paper_only: boolean;
  };
  positions: {
    paper_trade_id: number;
    symbol: string;
    strategy: string;
    side: string;
    quantity: number;
    entry_price: number;
    latest_price: number;
    notional: number;
    exposure_pct: number;
    unrealized_pl: number;
    unrealized_pl_pct: number;
    entry_time: string | null;
  }[];
  symbol_exposure: {
    key: string;
    exposure_pct: number;
    limit_pct: number;
    utilization: number;
  }[];
  strategy_exposure: {
    key: string;
    exposure_pct: number;
    open_positions: number;
    position_limit: number;
    utilization: number;
  }[];
  alerts: {
    severity: "clear" | "warning" | "breach";
    label: string;
    message: string;
    utilization: number | null;
  }[];
  data_sources: string[];
};

export type PortfolioRiskActionResponse = {
  status: string;
  message: string;
  breach_labels: string[];
  previous_breach_labels: string[];
  persistent_labels: string[];
  actions_taken: {
    action: string;
    labels?: string[];
    affected_strategy_ids?: number[];
  }[];
  kill_switch_enabled: boolean;
  affected_strategy_ids: number[];
  snapshot: PortfolioRiskSnapshot;
};

export type PortfolioAllocationPlan = {
  generated_at: string;
  status: string;
  message: string;
  paper_equity: number;
  risk_limits: Record<string, unknown>;
  open_positions: number;
  gross_exposure: number;
  alerts: PortfolioRiskSnapshot["alerts"];
  memory_replay_gate: {
    status: string;
    allows_memory_increase: boolean;
    complete_samples: number;
    min_complete_samples: number;
    avg_return_delta: number | null;
    min_avg_return_delta: number;
    hit_rate_delta: number | null;
    min_hit_rate_delta: number;
    cumulative_return_delta: number | null;
    blockers: string[];
  };
  positive_candidates: number;
  recommendations: {
    symbol: string;
    strategy: string;
    strategy_name: string;
    strategy_status: string;
    candidate_status: string;
    market_regime: string;
    paper_trade_id: number | null;
    rank_score: number;
    allocation_score: number;
    probability_up: number;
    expected_return: number;
    current_exposure_pct: number;
    base_target_exposure_pct: number;
    target_exposure_pct: number;
    memory_allocation_multiplier: number;
    memory_allocation_context: {
      multiplier: number;
      requested_multiplier: number;
      gate_limited: boolean;
      cap: number;
      score_adjustment: number;
      review_threshold_adjustment: number;
      replay_gate: PortfolioAllocationPlan["memory_replay_gate"];
      replay_gate_scope: string;
      replay_gate_scope_label: string;
      status: string;
      sample_size: number;
      notes: string;
    };
    symbol_exposure_pct: number;
    symbol_limit_pct: number;
    symbol_room_pct: number;
    recommendation: "add" | "activate_candidate" | "trim" | "hold" | "wait_for_room" | "watch";
    reason: string;
  }[];
};

export type AllocationReviewQueue = {
  generated_at: string;
  status: string;
  message: string;
  open_gate_alerts: number;
  actionable_items: number;
  allocation_plan_generated_at: string;
  items: {
    notification_id: number;
    gate_scope: string;
    gate_key: string;
    gate_label: string;
    gate_status: string;
    gate_complete_samples: number | null;
    gate_min_complete_samples: number | null;
    symbol: string;
    strategy: string;
    strategy_name: string;
    market_regime: string;
    recommendation: "add" | "activate_candidate" | "trim" | "hold" | "wait_for_room" | "watch";
    review_status: string;
    paper_action: "add" | "trim" | null;
    dry_run_available: boolean;
    rank_score: number;
    probability_up: number;
    expected_return: number;
    current_exposure_pct: number;
    target_exposure_pct: number;
    base_target_exposure_pct: number;
    memory_allocation_multiplier: number;
    requested_memory_multiplier: number;
    memory_status: string;
    reason: string;
    notification_created_at: string;
    paper_only: boolean;
  }[];
};

export type AllocationReviewResult = {
  status: string;
  message: string;
  decision: "approve" | "skip";
  queue_item: AllocationReviewQueue["items"][number];
  journal_entry: CandidateDecisionJournalEntry;
  paper_only: boolean;
};

export type AllocationReviewDryRunResult = PortfolioAllocationExecution & {
  journal_entry_id: number;
  approved_review: {
    id: number;
    symbol: string;
    strategy_type: string;
    decision: "approve";
    status: string;
    reason: string | null;
    created_at: string;
  };
  queue_item: AllocationReviewQueue["items"][number];
  paper_only: boolean;
};

export type PortfolioAllocationExecution = {
  generated_at: string;
  status: string;
  dry_run: boolean;
  message: string;
  actions: {
    action: string;
    symbol: string;
    strategy: string;
    paper_trade_id?: number | null;
    reduce_pct?: number;
    dry_run: boolean;
    reason: string;
    result?: unknown;
  }[];
  skipped: Record<string, unknown>[];
  readiness: Record<string, unknown>;
  plan: PortfolioAllocationPlan;
};

export type PaperTradingRunResponse = {
  symbol: string;
  strategy: string;
  action: string;
  approved: boolean;
  reason: string;
  signal_id: number | null;
  paper_trade_id: number | null;
  price: number;
  quantity: number;
  confidence: number;
  broker_order: BrokerOrderResponse | null;
};

export type PaperTradeReconcileResponse = {
  checked: number;
  closed: number;
  closed_trade_ids: number[];
};

export type PaperTradeReduceResponse = {
  paper_trade_id: number;
  status: string;
  reduced_quantity: number;
  remaining_quantity: number;
  realized_profit_loss: number;
  realized_profit_loss_pct: number;
  exit_price: number;
  reason: string;
  broker_order: BrokerOrderResponse | null;
};

export type StrategyMemory = {
  id: number;
  strategy_id: number | null;
  market_regime: string | null;
  symbol: string | null;
  sample_size: number | null;
  avg_return: string | number | null;
  avg_drawdown: string | number | null;
  win_rate: string | number | null;
  profit_factor: string | number | null;
  confidence_score: string | number | null;
  last_updated: string;
  notes: string | null;
};

export type AuditLog = {
  id: number;
  event_type: string;
  entity_type: string | null;
  entity_id: number | null;
  action: string;
  status: string;
  message: string | null;
  payload: Record<string, unknown> | null;
  created_at: string;
};

export type AuditLogFilters = {
  event_type?: string;
  action?: string;
  entity_type?: string;
};

export type NotificationItem = {
  id: number;
  category: string;
  severity: "info" | "warning" | "critical" | string;
  status: "open" | "acknowledged" | "resolved" | string;
  source: string;
  title: string;
  message: string | null;
  entity_type: string | null;
  entity_id: number | null;
  payload: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  acknowledged_at: string | null;
  resolved_at: string | null;
};

export type ReadinessSnapshot = {
  generated_at: string;
  overall_status: "ready" | "warning" | "blocked" | string;
  summary: {
    ready: number;
    warning: number;
    blocked: number;
  };
  checks: {
    name: string;
    status: "ready" | "warning" | "blocked" | string;
    message: string;
    details: Record<string, unknown>;
  }[];
  paper_trading_allowed: boolean;
};

export type DeploymentMonitorSnapshot = {
  generated_at: string;
  environment: string;
  deployable: boolean;
  status: "ready" | "warning" | "blocked" | string;
  paper_trading_allowed: boolean;
  live_trading_allowed: boolean;
  message: string;
  checks: ReadinessSnapshot["checks"];
  blockers: string[];
  warnings: string[];
  readiness_status: string;
  readiness_blockers: string[];
  readiness_warnings: string[];
  readiness: ReadinessSnapshot;
};

export type StockMonitoringCheck = {
  key: string;
  category: string;
  metric: string;
  status: "clear" | "warning" | "breach" | "unknown";
  message: string;
  value: Record<string, unknown>;
  threshold: Record<string, unknown>;
  details: Record<string, unknown>;
  action_scope: string;
};

export type StockMonitoringSnapshot = {
  status: string;
  generated_at: string;
  snapshot_id?: number;
  checks: StockMonitoringCheck[];
  breaches: { key: string; status: string; consecutive_count: number; severity: string; last_observed_at: string }[];
  actions: { action: string; reasons?: string[]; model_run_id?: string }[];
};

export type StockLearningCycleEvent = {
  id: number;
  stage: string;
  decision: string;
  actor: string;
  reason: string;
  decision_sha256: string;
  evidence: Record<string, unknown>;
  created_at: string;
};
export type TradeCandidate = {
  symbol: string;
  strategy: string;
  strategy_name: string;
  strategy_status: string;
  strategy_research: {
    source_label: string;
    source_url: string | null;
    idea: string;
    ideal_market: string;
    confirmation_rules: string[];
    risk_notes: string;
  };
  candidate_status: "positive_candidate" | "needs_more_evidence" | "watch" | string;
  base_score: number | null;
  score: number;
  memory_score_adjustment: number;
  review_threshold_adjustment: number;
  journal_feedback: {
    source: string;
    status: string;
    sample_size: number;
    score_adjustment: number;
    review_threshold_adjustment: number;
    confidence_score: number | null;
    avg_return: number | null;
    win_rate: number | null;
    notes: string;
  };
  action: string;
  probability_up: number;
  expected_return: number;
  horizon_days: number | null;
  confidence: number;
  backtest_score: number;
  backtest_rejected: boolean;
  blockers: string[];
  reason: string;
  news_summary: string;
  macro_summary: string;
  market_regime: string;
  data_source: string;
};

export type TradeCandidateResponse = {
  generated_at: string;
  cache_status: "fresh" | "cached" | string;
  cached_at: string | null;
  candidate_count: number;
  positive_count: number;
  candidates: TradeCandidate[];
};

export type TradeCandidateEvidence = {
  generated_at: string;
  symbol: string;
  strategy: string;
  strategy_name: string;
  strategy_status: string;
  cached_candidate: TradeCandidate & { scanner_cache_status?: string; scanner_generated_at?: string };
  market_data: {
    source: string;
    rows_used: number;
    latest_close: number;
    latest_date: string | null;
  };
  model_evidence: {
    source: string;
    rows_used: number;
    prediction_date: string | null;
    latest_features: Record<string, number>;
    predictions: { horizon_days: number; probability_up: number; probability_down: number; expected_return: number; probabilities_by_model: Record<string, number> }[];
    walk_forward: { horizon_days: number; fold: number; accuracy: number; brier_score: number; sample_size: number; train_start: string; train_end: string; test_start: string; test_end: string }[];
    warnings: string[];
  };
  signal_evidence: {
    action: string;
    probability_up: number;
    probability_down: number;
    confidence: number;
    reason: string;
    features: Record<string, unknown>;
  };
  backtest_evidence: {
    score: number;
    rejected: boolean;
    rejection_reasons: string[];
    total_return: number;
    annualized_return: number;
    sharpe_ratio: number;
    sortino_ratio: number;
    max_drawdown: number;
    win_rate: number;
    profit_factor: number;
    number_of_trades: number;
  };
  context_evidence: {
    news: NewsSentimentSummary;
    macro: MacroContextSummary;
    market_regime: string;
  };
  risk_room: {
    paper_equity: number;
    open_positions: number;
    gross_exposure: number;
    symbol_exposure_pct: number;
    symbol_limit_pct: number;
    symbol_room_pct: number;
    readiness_status: string;
    paper_trading_allowed: boolean;
    blocked_checks: string[];
    allocation_recommendation: string;
    allocation_reason: string;
    target_exposure_pct: number | null;
    current_exposure_pct: number | null;
    alerts: { severity: string; label: string; message: string; utilization: number | null }[];
  };
};

export type ScannerRefreshJob = {
  id: number;
  trigger: string;
  status: "queued" | "running" | "complete" | "failed" | string;
  message: string | null;
  payload: {
    limit?: number;
    context?: Record<string, unknown>;
    candidate_count?: number;
    positive_count?: number;
    cache_status?: string;
    generated_at?: string;
    top_candidates?: Pick<TradeCandidate, "symbol" | "strategy" | "strategy_name" | "candidate_status" | "score">[];
    error?: string;
  };
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
};

export type TradeScorecardRow = {
  paper_trade_id: number;
  symbol: string;
  strategy_name: string;
  strategy_type: string;
  status: string;
  entry_time: string | null;
  exit_time: string | null;
  entry_price: number;
  current_price: number;
  quantity: number;
  profit_loss: number;
  profit_loss_pct: number;
  age_days: number;
  probability_up_at_entry: number;
  expected_return_at_entry: number;
  horizon_days: number | null;
  predictive_model_supported: boolean;
  on_track: boolean;
  reason_entered: string | null;
  reason_exited: string | null;
};

export type TradeScorecardResponse = {
  generated_at: string;
  paper_trade_count: number;
  open_trades: number;
  closed_trades: number;
  realized_win_rate: number | null;
  avg_realized_return: number | null;
  avg_open_return: number | null;
  positive_open_trades: number;
  rows: TradeScorecardRow[];
};

export type CandidateActivationResponse = {
  symbol: string;
  strategy: string;
  strategy_name: string;
  decision: string;
  status: string;
  message: string;
  old_status: string;
  new_status: string;
  eligible: boolean;
  blockers: string[];
  candidate: Record<string, unknown>;
  review_context: Record<string, unknown>;
  journal_entry_id: number | null;
};

export type CandidateDecisionJournalEntry = {
  id: number;
  symbol: string;
  strategy_id: number | null;
  strategy_type: string;
  decision: string;
  status: string;
  reason: string | null;
  evidence_snapshot: Record<string, unknown>;
  paper_trade_id: number | null;
  realized_return: number | null;
  realized_status: string | null;
  created_at: string;
};

export type CandidateDecisionScorecard = {
  journal_count: number;
  scored_count: number;
  positive_outcomes: number;
  hit_rate: number | null;
  avg_outcome_return: number | null;
  by_decision: {
    decision: string;
    count: number;
    scored: number;
    positive: number;
    hit_rate: number | null;
    avg_return: number | null;
    avg_score: number | null;
  }[];
  rows: {
    id: number;
    symbol: string;
    strategy_type: string;
    decision: string;
    status: string;
    created_at: string;
    probability_up: number;
    expected_return: number;
    outcome_return: number | null;
    outcome_status: string | null;
    quality: string;
    score: number | null;
    market_follow_through: Record<string, unknown>;
  }[];
};

export type MemoryReplayResponse = {
  generated_at: string;
  top_k: number;
  evaluated_rows: number;
  complete_rows: number;
  pending_rows: number;
  risk_profile: Record<string, number>;
  baseline: {
    selected: number;
    complete: number;
    pending: number;
    hit_rate: number | null;
    avg_return: number | null;
    cumulative_return: number | null;
    max_drawdown: number | null;
  };
  memory_adjusted: {
    selected: number;
    complete: number;
    pending: number;
    hit_rate: number | null;
    avg_return: number | null;
    cumulative_return: number | null;
    max_drawdown: number | null;
  };
  delta: {
    selected: number;
    complete: number;
    hit_rate: number | null;
    avg_return: number | null;
    cumulative_return: number | null;
    max_drawdown: number | null;
  };
  replay_gate: PortfolioAllocationPlan["memory_replay_gate"];
  groups: {
    by_symbol_strategy: MemoryReplayGroup[];
    by_symbol: MemoryReplayGroup[];
    by_strategy: MemoryReplayGroup[];
    by_regime: MemoryReplayGroup[];
  };
  approval_alerts: {
    checked_open_gates: number;
    created: number;
    updated: number;
    resolved: number;
    open_gates: {
      scope: string;
      scope_key: string;
      scope_label: string;
      row_count: number | null;
      replay_gate: PortfolioAllocationPlan["memory_replay_gate"];
    }[];
  };
  rows: {
    source: string;
    source_id: number;
    generated_at: string;
    symbol: string;
    strategy: string;
    strategy_name: string;
    market_regime: string;
    base_score: number;
    memory_score: number;
    memory_score_adjustment: number;
    review_threshold_adjustment: number;
    baseline_rank: number;
    memory_rank: number;
    baseline_selected: boolean;
    memory_selected: boolean;
    base_threshold: number;
    adjusted_threshold: number;
    outcome_status: string;
    outcome_return: number | null;
  }[];
};

export type MemoryReplayGroup = {
  key: string;
  label: string;
  row_count: number;
  baseline: MemoryReplayResponse["baseline"];
  memory_adjusted: MemoryReplayResponse["memory_adjusted"];
  delta: MemoryReplayResponse["delta"];
  replay_gate: PortfolioAllocationPlan["memory_replay_gate"];
};

export type StrategyGovernanceResponse = {
  evaluated: number;
  changed: number;
  thresholds: Record<string, Record<string, number>>;
  decisions: {
    strategy_id: number;
    strategy_name: string;
    strategy_type: string;
    old_status: string;
    new_status: string;
    decision: string;
    reason: string;
    memory: Record<string, number | string | null>;
    checks: Record<string, {
      label: string;
      passed: boolean;
      actual: number;
      threshold: number;
      comparator: string;
    }[]>;
    thresholds: Record<string, Record<string, number>>;
  }[];
  comparison?: StrategyGovernanceComparison | null;
  notifications?: {
    created: number;
    notifications: {
      id: number;
      severity: string;
      source: string;
      strategy_id: number | null;
    }[];
  } | null;
};

export type StrategyGovernanceComparison = {
  status: string;
  message: string;
  decision_changes: {
    strategy_id: number;
    strategy_name: string;
    change_type: string;
    previous_decision: string | null;
    current_decision: string | null;
    previous_status: string | null;
    current_status: string | null;
  }[];
  memory_changes: {
    strategy_id: number;
    strategy_name: string;
    metrics: Record<string, { previous: number | null; current: number | null; delta: number | null }>;
  }[];
  threshold_changes: {
    group: string;
    metric: string;
    previous: number | null;
    current: number | null;
  }[];
};

export type StrategyGovernanceScorecardSnapshot = {
  status: string;
  message: string | null;
  scorecard: StrategyGovernanceResponse | null;
  audit_log_id: number | null;
  created_at: string | null;
  previous_audit_log_id: number | null;
  previous_created_at: string | null;
  comparison: StrategyGovernanceComparison;
};

export type StrategyReactivationCandidate = {
  strategy_id: number;
  strategy_name: string;
  strategy_type: string;
  current_status: string;
  eligible: boolean;
  blockers: string[];
  memory: {
    sample_size?: number;
    win_rate?: number;
    profit_factor?: number;
    avg_drawdown?: number;
    confidence_score?: number;
    market_regime?: string;
    symbol?: string | null;
  };
};

export type StrategyReactivationQueue = {
  kill_switch_enabled: boolean;
  candidates: StrategyReactivationCandidate[];
};

export type StrategyReactivationReviewResponse = {
  strategy_id: number;
  strategy_name: string;
  decision: "approve" | "reject" | "hold";
  status: string;
  message: string;
  old_status: string;
  new_status: string;
  eligible: boolean;
  blockers: string[];
  memory: StrategyReactivationCandidate["memory"];
};

export type PersistedModelRunResponse = ModelPredictionResponse & {
  saved_prediction_ids: number[];
  saved_validation_fold_ids: number[];
};

export type ModelPerformanceResponse = {
  total_predictions: number;
  realized_predictions: number;
  avg_brier_score: number | null;
  hit_rate: number | null;
  predictions: {
    id: number;
    symbol: string;
    prediction_date: string;
    horizon_days: number;
    probability_up: string | number;
    probability_down: string | number;
    expected_return: string | number;
    source: string;
    realized_return: string | number | null;
    realized_up: boolean | null;
    brier_score: string | number | null;
    is_realized: boolean;
    created_at: string;
  }[];
};

export type ModelRealizationScoreResponse = {
  checked: number;
  scored: number;
  scored_prediction_ids: number[];
};

export type StrategyExperiment = {
  id: number;
  strategy_id: number | null;
  experiment_name: string | null;
  old_parameters: Record<string, unknown> | null;
  new_parameters: Record<string, unknown> | null;
  hypothesis: string | null;
  backtest_result_id: number | null;
  paper_result_summary: {
    symbol?: string;
    source?: string;
    old_score?: number;
    new_score?: number;
    delta_score?: number;
    reason?: string;
    old_metrics?: Record<string, number>;
    new_metrics?: Record<string, number>;
  } | null;
  decision: string | null;
  created_at: string;
};

export type StrategyExperimentRunResponse = {
  symbol: string;
  strategy: string;
  source: string;
  baseline_backtest_id: number;
  baseline_score: number;
  applied_parameters: Record<string, unknown> | null;
  experiments: StrategyExperiment[];
};

export type StrategyImprovementQueue = {
  queued: number;
  retired: number;
  paused: number;
  rows: {
    strategy_id: number;
    strategy_name: string;
    strategy_type: string;
    current_status: string;
    severity: string;
    symbol: string;
    market_regime: string;
    memory: Record<string, number | string | null>;
    improvement_reasons: string[];
    proposed_experiments: {
      experiment_name: string;
      old_parameters?: Record<string, unknown>;
      new_parameters: Record<string, unknown>;
      hypothesis: string;
      decision: string;
    }[];
    paper_gates: {
      name: string;
      status: string;
      requirement: string;
    }[];
    latest_experiment: {
      id: number;
      decision: string | null;
      experiment_name: string | null;
      created_at: string;
      paper_result_summary: Record<string, unknown> | null;
    } | null;
    recommended_action: string;
    paper_only: boolean;
  }[];
};

export type MarketRegime = {
  id: number;
  regime_date: string;
  spy_trend: string | null;
  volatility_regime: string | null;
  rate_regime: string | null;
  market_regime: string | null;
  features: Record<string, number | string>;
};

export type NewsArticle = {
  id: number;
  symbol: string | null;
  published_at: string | null;
  source: string | null;
  title: string | null;
  url: string | null;
  summary: string | null;
  sentiment_score: string | number | null;
  relevance_score: string | number | null;
  raw_payload: Record<string, unknown> | null;
};

export type NewsSentimentSummary = {
  symbol: string;
  article_count: number;
  average_sentiment: number;
  sentiment_label: string;
  summary: string;
  top_headlines: {
    id: number;
    title: string | null;
    source: string | null;
    published_at: string | null;
    sentiment_score: number;
    relevance_score: number;
  }[];
};

export type NewsImportResponse = {
  symbol: string;
  rows_imported: number;
  article_ids: number[];
  source: string;
  error: string | null;
};

export type CompanyOpportunityRadar = {
  generated_at: string;
  asset_count: number;
  scanner_cache_status: string | null;
  news_imports: NewsImportResponse[];
  opportunities: {
    symbol: string;
    name: string;
    sector: string | null;
    data_source: string;
    opportunity_score: number;
    recommendation: string;
    positive_candidate_count: number;
    price: {
      latest_close: number;
      return_20d: number;
      return_60d: number;
      ma_20: number;
      ma_50: number;
      trend_label: string;
      chart: { date: string; close: number }[];
    };
    news: NewsSentimentSummary;
    best_candidate: TradeCandidate | null;
  }[];
};

export type WatchlistDiscovery = {
  generated_at: string;
  source: string;
  candidate_count: number;
  candidates: {
    symbol: string;
    name: string;
    sector: string;
    industry: string | null;
    already_active: boolean;
  }[];
};

export type WatchlistImportResponse = {
  generated_at: string;
  source: string;
  activated_symbols: string[];
  market_results: MarketImportResponse[];
  news_results: NewsImportResponse[];
  candidate_scan: {
    cache_status: string;
    candidate_count: number;
    positive_count: number;
    generated_at: string;
    top_candidates: Pick<TradeCandidate, "symbol" | "strategy" | "strategy_name" | "candidate_status" | "score">[];
  } | null;
};

export type EconomicIndicator = {
  id: number;
  indicator_name: string;
  observation_date: string;
  value: string | number | null;
  unit: string | null;
  source: string | null;
  raw_payload: Record<string, unknown> | null;
  created_at: string;
};

export type EconomicImportResponse = {
  source: string;
  rows_imported: number;
  indicator_ids: number[];
};

export type MacroContextSummary = {
  source: string;
  indicator_count: number;
  macro_label: string;
  rate_regime: string;
  inflation_regime: string;
  labor_regime: string;
  volatility_regime: string;
  summary: string;
  latest: Record<string, { value: number; unit: string | null; observation_date: string; source: string | null }>;
};

export type BrokerStatus = {
  paper_broker: string;
  paper_trading_enabled: boolean;
  live_trading_enabled: boolean;
  live_trading_blocked: boolean;
  message: string;
};

export type StockPaperSignalResponse = {
  mode: "paper";
  action: string;
  signal_id: number;
  symbol: string;
  strategy: string;
  signal_action: string;
  confidence: string;
  reference_price: string;
  execution_created: false;
};

export type BrokerOrderResponse = {
  broker: string;
  broker_order_id: string | null;
  symbol: string | null;
  side: string | null;
  quantity: number | null;
  order_type: string | null;
  time_in_force: string | null;
  status: string;
  paper_only: boolean;
  submitted_at: string | null;
  source: string | null;
  paper_trade_id: number | null;
  signal_id: number | null;
  live_trading_enabled: boolean | null;
  reason: string | null;
};

export type SafetyControlResponse = {
  action: string;
  status: string;
  message: string;
  kill_switch_enabled: boolean;
  paused_strategies: number;
  affected_strategy_ids: number[];
  risk_rule: { id: number; name: string; value: Record<string, unknown>; is_active: boolean } | null;
};

export type RiskRule = {
  id?: number;
  name: string;
  value: Record<string, unknown>;
  is_active: boolean;
};

export type RiskSettingsUpdate = {
  min_confidence?: number;
  max_daily_drawdown?: number;
  max_strategy_drawdown?: number;
  max_open_positions?: number;
  max_open_positions_per_strategy?: number;
  max_symbol_exposure?: number;
  max_risk_per_trade?: number;
  stop_after_consecutive_losses?: number;
  candidate_review_score_threshold?: number;
  activation_score_threshold?: number;
  journal_feedback_review_threshold_cap?: number;
  journal_feedback_allocation_multiplier_cap?: number;
  memory_replay_min_complete_samples?: number;
  memory_replay_min_avg_return_delta?: number;
  memory_replay_min_hit_rate_delta?: number;
  reason?: string;
};

/**
 * Stock training is deliberately a separate API surface from the legacy
 * experimental research endpoints above.  These types mirror the durable
 * job/report contract and keep all identifiers opaque to the UI.
 */
export type StockTrainingJobStatus =
  | "queued"
  | "running"
  | "complete"
  | "completed"
  | "failed"
  | "succeeded"
  | "cancel_requested"
  | "cancelled"
  | "canceled"
  | "pending"
  | string;
const configuredApi = import.meta.env.VITE_API_BASE_URL ?? "/api";
const API_BASE_URL = configuredApi.replace(/\/$/, "");

let accessToken = "";
let authMode: AuthConfig["mode"] = "local_role_keys";
export function setAccessToken(value: string) { accessToken = value; }
export function setAuthMode(value: AuthConfig["mode"]) { authMode = value; }
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string | null;

  constructor(status: number, detail: string | null = null) {
    super(apiErrorMessage(status, detail));
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function apiErrorMessage(status: number, detail: string | null): string {
  if (status === 401) return "Your sign-in has expired or is invalid. Please sign in again.";
  if (status === 403) return "Permission required for this action.";
  if (status === 503) return "A required dependency or configuration is unavailable. Try again later.";
  if (status === 409) return detail || "This action conflicts with the current safety state.";
  return detail || `Request could not be completed (${status}).`;
}

export function getErrorMessage(error: unknown, fallback = "Something went wrong."): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message && error.message !== "[object Object]") return error.message;
  return fallback;
}

async function responseDetail(response: Response): Promise<string | null> {
  try {
    const payload: unknown = await response.clone().json();
    if (typeof payload === "string") return payload;
    if (payload && typeof payload === "object") {
      const record = payload as Record<string, unknown>;
      for (const key of ["detail", "reason", "message", "error"]) {
        if (typeof record[key] === "string") return record[key] as string;
      }
    }
  } catch {
    // Non-JSON error bodies are intentionally not surfaced.
  }
  return null;
}

async function handleResponse<T>(response: Response): Promise<T> {
  if (!response.ok) throw new ApiError(response.status, await responseDetail(response));
  return response.json() as Promise<T>;
}

async function authenticatedFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  if (!accessToken && authMode !== "production_identity") throw new Error("Sign in to access the trading service.");
  const headers = new Headers(init.headers);
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  if (authMode === "production_identity" && init.method && init.method !== "GET") {
    headers.set("X-Action-Confirmation", "confirm");
    headers.set("X-Idempotency-Key", globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`);
    try {
      const body = typeof init.body === "string" ? JSON.parse(init.body) as Record<string, unknown> : null;
      const reason = body && typeof body.reason === "string" ? body.reason : "Explicit control-room action";
      headers.set("X-Action-Reason", reason);
    } catch {
      headers.set("X-Action-Reason", "Explicit control-room action");
    }
  }
  const response = await globalThis.fetch(input, { ...init, headers, cache: "no-store", credentials: authMode === "production_identity" ? "include" : "omit" });
  if (!response.ok) throw new ApiError(response.status, await responseDetail(response));
  return response;
}

export async function getDashboard(): Promise<DashboardSnapshot> {
  const response = await authenticatedFetch(`${API_BASE_URL}/dashboard`);
  return handleResponse<DashboardSnapshot>(response);
}

export async function getAuthSession(): Promise<AuthSession> {
  const response = await authenticatedFetch(`${API_BASE_URL}/auth/session`);
  return handleResponse<AuthSession>(response);
}

export async function getAuthConfig(): Promise<AuthConfig> {
  const response = await globalThis.fetch(`${API_BASE_URL}/auth/config`, {
    cache: "no-store",
    credentials: "include",
  });
  return handleResponse<AuthConfig>(response);
}

export type ResearchRunSummary = {
  run_id: string;
  status: "experimental";
  eligible_for_trading: false;
  symbol: string;
  horizon_bars: number;
  train_rows: number;
  holdout_rows: number;
  holdout_start: string;
  holdout_end: string;
  metrics: Record<string, { brier_score: number; log_loss: number }>;
  versions: Record<string, string>;
};

export async function getResearchRuns(): Promise<{ items: ResearchRunSummary[]; total: number }> {
  const response = await authenticatedFetch(`${API_BASE_URL}/research/runs?limit=20`);
  return handleResponse<{ items: ResearchRunSummary[]; total: number }>(response);
}

export type AgentResearchResult = {
  recommendation: "BUY" | "SELL" | "HOLD";
  rationale: string;
  confidence: number;
  limitations: string[];
  decision_at?: string;
};

export type AgentResearchRun = {
  run_id: string;
  symbol: "AAPL" | "MSFT" | "QQQ" | "SPY" | string;
  status: "queued" | "running" | "completed" | "failed" | "unavailable" | string;
  eligible_for_trading: false;
  framework_version: string;
  prompt_version: string;
  model_name: string;
  requested_by: string;
  source_snapshot: Array<Record<string, unknown>>;
  result: AgentResearchResult | null;
  evaluation: {
    status: string;
    horizon_days?: number;
    baseline?: string | Record<string, unknown>;
    pending_reason?: string;
    qualification?: string;
    source_cutoff?: string;
    outcome_date?: string;
    outcome_return?: number;
    outcome_up?: boolean;
    agent?: { recommendation: string; directional_correct: boolean | null; directional_score_status: string };
    baseline_result?: Record<string, unknown>;
    comparison?: { status: string; reason?: string | null };
  };
  usage: Record<string, unknown>;
  error: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
};

export async function getAgentResearchRuns(): Promise<{ items: AgentResearchRun[]; total: number }> {
  const response = await authenticatedFetch(`${API_BASE_URL}/research/agent-runs?limit=20`, { cache: "no-store" });
  return handleResponse<{ items: AgentResearchRun[]; total: number }>(response);
}

export async function startAgentResearch(symbol: string): Promise<AgentResearchRun & { deduplicated: boolean }> {
  return postJson<AgentResearchRun & { deduplicated: boolean }>("/research/agent-runs", { symbol });
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await authenticatedFetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  return handleResponse<T>(response);
}

async function patchJson<T>(path: string, body: unknown): Promise<T> {
  const response = await authenticatedFetch(`${API_BASE_URL}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  return handleResponse<T>(response);
}

export async function importMarketData(symbol: string, period = "2y"): Promise<MarketImportResponse> {
  return postJson<MarketImportResponse>("/market-data/import", { symbol, period });
}

/** Imports completed regular-session one-minute bars; intentionally separate from Yahoo history. */
export async function importIntradayMarketData(symbol: string): Promise<IntradayImportResponse> {
  return postJson<IntradayImportResponse>("/market-data/intraday/ingest", { symbols: [symbol] });
}

export async function runIntradayPreflight(): Promise<IntradayPreflightResponse> {
  return postJson<IntradayPreflightResponse>("/market-data/intraday/preflight", {});
}
export async function getMarketDataHealth(symbol: string): Promise<MarketDataHealth> {
  const response = await authenticatedFetch(`${API_BASE_URL}/market-data/intraday/${encodeURIComponent(symbol)}/status`, { cache: "no-store" });
  return handleResponse<MarketDataHealth>(response);
}

export async function getWatchlistDiscovery(limit = 10): Promise<WatchlistDiscovery> {
  const response = await authenticatedFetch(`${API_BASE_URL}/watchlist/discover?limit=${limit}`, { cache: "no-store" });
  return handleResponse<WatchlistDiscovery>(response);
}

export async function importWatchlist(limit = 6, newsProvider: "auto" | "yfinance" | "nasdaq_rss" | "mock" = "auto", refreshCandidates = false): Promise<WatchlistImportResponse> {
  return postJson<WatchlistImportResponse>("/watchlist/import", {
    symbols: [],
    limit,
    period: "2y",
    import_prices: true,
    import_news: true,
    news_provider: newsProvider,
    refresh_candidates: refreshCandidates,
  });
}

export async function getPriceHistory(symbol: string, limit = 260): Promise<PriceHistoryResponse> {
  const response = await authenticatedFetch(`${API_BASE_URL}/market-data/${encodeURIComponent(symbol)}?limit=${limit}`, { cache: "no-store" });
  return handleResponse<PriceHistoryResponse>(response);
}

export async function runBacktest(symbol: string, strategy: string): Promise<BacktestResponse> {
  return postJson<BacktestResponse>("/backtests", { symbol, strategy });
}

export async function runModelPrediction(symbol: string): Promise<ModelPredictionResponse> {
  return postJson<ModelPredictionResponse>("/models/predict", { symbol });
}

export async function generateStockPaperSignal(symbol: string, strategy: string): Promise<StockPaperSignalResponse> {
  return postJson<StockPaperSignalResponse>("/stock-paper/signal", { symbol, strategy });
}

export async function runPaperSignal(symbol: string, strategy: string): Promise<PaperTradingRunResponse> {
  return postJson<PaperTradingRunResponse>("/paper-trading/run-signal", { symbol, strategy });
}

export async function reconcilePaperTrades(): Promise<PaperTradeReconcileResponse> {
  return postJson<PaperTradeReconcileResponse>("/paper-trading/reconcile", {});
}

export async function closePaperTrade(tradeId: number): Promise<{ paper_trade_id: number; status: string; profit_loss: number; profit_loss_pct: number; reason: string }> {
  return postJson(`/paper-trading/close/${tradeId}`, {});
}

export async function reducePaperTrade(tradeId: number, reducePct: number, reason: string): Promise<PaperTradeReduceResponse> {
  return postJson<PaperTradeReduceResponse>(`/paper-trading/reduce/${tradeId}`, { reduce_pct: reducePct, reason });
}

export async function getPaperTrades(): Promise<PaperTrade[]> {
  const response = await authenticatedFetch(`${API_BASE_URL}/paper-trades`, { cache: "no-store" });
  return handleResponse<PaperTrade[]>(response);
}

export async function getStockPaperStatus(): Promise<StockPaperStatus> {
  const response = await authenticatedFetch(`${API_BASE_URL}/stock-paper/status`, { cache: "no-store" });
  return handleResponse<StockPaperStatus>(response);
}

export async function initializeStockPaperAccount(): Promise<StockPaperStatus> {
  return postJson<StockPaperStatus>("/stock-paper/initialize", {});
}

export async function reconcileStockPaperAccount(): Promise<StockPaperStatus> {
  return postJson<StockPaperStatus>("/stock-paper/reconcile", {});
}

export async function haltStockPaperAccount(reason: string): Promise<StockPaperStatus> {
  return postJson<StockPaperStatus>("/stock-paper/halt", { reason });
}

export async function resumeStockPaperAccount(): Promise<StockPaperStatus> {
  return postJson<StockPaperStatus>("/stock-paper/resume", {});
}

export async function getStockPaperRecovery(): Promise<StockPaperRecoveryStatus> {
  const response = await authenticatedFetch(`${API_BASE_URL}/stock-paper/recovery`, { cache: "no-store" });
  return handleResponse<StockPaperRecoveryStatus>(response);
}

export async function cancelStockPaperRecovery(flattenPolicy: "none" | "positions", reason: string): Promise<StockPaperRecoveryStatus> {
  return postJson<StockPaperRecoveryStatus>("/stock-paper/recovery/cancel", {
    flatten_policy: flattenPolicy,
    reason,
  });
}

export async function rollbackStockPaperToLastKnownGood(reason: string): Promise<StockPaperRecoveryStatus> {
  return postJson<StockPaperRecoveryStatus>("/stock-paper/recovery/rollback", { reason });
}

export async function acknowledgeStockPaperAccountingReview(reason: string): Promise<StockPaperRecoveryStatus> {
  return postJson<StockPaperRecoveryStatus>("/stock-paper/recovery/accounting-review", {
    reason,
    confirm_residual_review: true,
  });
}

export type StockPaperOrderRequest = {
  symbol: string;
  side: "buy" | "sell";
  quantity: string;
  reference_price: string;
  idempotency_key: string;
  signal_id?: number;
  source?: string;
};

export async function reserveStockPaperOrder(request: StockPaperOrderRequest): Promise<StockPaperOrderActionResponse> {
  return postJson<StockPaperOrderActionResponse>("/stock-paper/orders", request);
}

export async function dispatchStockPaperOrder(orderId: number): Promise<StockPaperOrderActionResponse> {
  return postJson<StockPaperOrderActionResponse>(`/stock-paper/orders/${orderId}/dispatch`, {});
}

export async function closeStockPaperPosition(symbol: string, idempotencyKey: string): Promise<StockPaperOrderActionResponse> {
  return postJson<StockPaperOrderActionResponse>(`/stock-paper/positions/${encodeURIComponent(symbol)}/close`, {
    idempotency_key: idempotencyKey,
  });
}

export async function reduceStockPaperPosition(symbol: string, reducePct: string, idempotencyKey: string): Promise<StockPaperOrderActionResponse> {
  return postJson<StockPaperOrderActionResponse>(`/stock-paper/positions/${encodeURIComponent(symbol)}/reduce`, {
    reduce_pct: reducePct,
    idempotency_key: idempotencyKey,
  });
}

export async function getPortfolioRisk(): Promise<PortfolioRiskSnapshot> {
  const response = await authenticatedFetch(`${API_BASE_URL}/portfolio/risk`, { cache: "no-store" });
  return handleResponse<PortfolioRiskSnapshot>(response);
}

export async function getPortfolioAllocationPlan(limit = 20): Promise<PortfolioAllocationPlan> {
  const response = await authenticatedFetch(`${API_BASE_URL}/portfolio/allocation-plan?limit=${limit}`, { cache: "no-store" });
  return handleResponse<PortfolioAllocationPlan>(response);
}

export async function getAllocationReviewQueue(limit = 20): Promise<AllocationReviewQueue> {
  const response = await authenticatedFetch(`${API_BASE_URL}/portfolio/allocation-review-queue?limit=${limit}`, { cache: "no-store" });
  return handleResponse<AllocationReviewQueue>(response);
}

export async function reviewAllocationQueueItem(
  item: Pick<AllocationReviewQueue["items"][number], "notification_id" | "symbol" | "strategy">,
  decision: "approve" | "skip",
  reason: string
): Promise<AllocationReviewResult> {
  return postJson<AllocationReviewResult>("/portfolio/allocation-review-queue/review", {
    notification_id: item.notification_id,
    symbol: item.symbol,
    strategy: item.strategy,
    decision,
    reason
  });
}

export async function dryRunApprovedAllocationReview(
  item: Pick<AllocationReviewQueue["items"][number], "symbol" | "strategy">,
  journalEntryId?: number,
  limit = 20
): Promise<AllocationReviewDryRunResult> {
  return postJson<AllocationReviewDryRunResult>("/portfolio/allocation-review-queue/dry-run", {
    symbol: item.symbol,
    strategy: item.strategy,
    journal_entry_id: journalEntryId,
    limit
  });
}

export async function executePortfolioAllocationPlan(dryRun = true, maxActions = 3, limit = 20): Promise<PortfolioAllocationExecution> {
  return postJson<PortfolioAllocationExecution>("/portfolio/allocation-plan/execute", { dry_run: dryRun, max_actions: maxActions, limit });
}

export async function runPortfolioRiskActions(requirePersistence = true, dryRun = false): Promise<PortfolioRiskActionResponse> {
  return postJson<PortfolioRiskActionResponse>("/portfolio/risk/actions", { require_persistence: requirePersistence, dry_run: dryRun });
}

export async function getStrategyMemory(): Promise<StrategyMemory[]> {
  const response = await authenticatedFetch(`${API_BASE_URL}/strategy-memory`, { cache: "no-store" });
  return handleResponse<StrategyMemory[]>(response);
}

export async function evaluateStrategies(): Promise<StrategyGovernanceResponse> {
  return postJson<StrategyGovernanceResponse>("/strategies/evaluate", {});
}

export async function getLatestStrategyGovernanceScorecard(): Promise<StrategyGovernanceScorecardSnapshot> {
  const response = await authenticatedFetch(`${API_BASE_URL}/strategies/governance-scorecard`, { cache: "no-store" });
  return handleResponse<StrategyGovernanceScorecardSnapshot>(response);
}

export async function getStrategyImprovementQueue(): Promise<StrategyImprovementQueue> {
  const response = await authenticatedFetch(`${API_BASE_URL}/strategies/improvement-queue`, { cache: "no-store" });
  return handleResponse<StrategyImprovementQueue>(response);
}

export async function getStrategyReactivationQueue(): Promise<StrategyReactivationQueue> {
  const response = await authenticatedFetch(`${API_BASE_URL}/strategies/reactivation-queue`, { cache: "no-store" });
  return handleResponse<StrategyReactivationQueue>(response);
}

export async function reviewStrategyReactivation(strategyId: number, decision: "approve" | "reject" | "hold", reason: string): Promise<StrategyReactivationReviewResponse> {
  return postJson<StrategyReactivationReviewResponse>("/strategies/reactivation-review", { strategy_id: strategyId, decision, reason });
}

export async function getAuditLogs(limit = 25, filters: AuditLogFilters = {}): Promise<AuditLog[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  Object.entries(filters).forEach(([key, value]) => {
    if (value) {
      params.set(key, value);
    }
  });
  const response = await authenticatedFetch(`${API_BASE_URL}/audit-logs?${params.toString()}`, { cache: "no-store" });
  return handleResponse<AuditLog[]>(response);
}

export async function getNotifications(limit = 25, status = "open", category = ""): Promise<NotificationItem[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (status) {
    params.set("status", status);
  }
  if (category) {
    params.set("category", category);
  }
  const response = await authenticatedFetch(`${API_BASE_URL}/notifications?${params.toString()}`, { cache: "no-store" });
  return handleResponse<NotificationItem[]>(response);
}

export async function getReadiness(): Promise<ReadinessSnapshot> {
  const response = await authenticatedFetch(`${API_BASE_URL}/system/readiness`, { cache: "no-store" });
  return handleResponse<ReadinessSnapshot>(response);
}

export async function getDeploymentMonitor(): Promise<DeploymentMonitorSnapshot> {
  const response = await authenticatedFetch(`${API_BASE_URL}/system/deployment-monitor`, { cache: "no-store" });
  return handleResponse<DeploymentMonitorSnapshot>(response);
}

export async function getLiveOperations(): Promise<LiveOperationsSnapshot> {
  const response = await authenticatedFetch(`${API_BASE_URL}/system/live-operations`, { cache: "no-store" });
  return handleResponse<LiveOperationsSnapshot>(response);
}

export async function getLiveOperationsEvidence(limit = 50): Promise<Record<string, unknown>> {
  const response = await authenticatedFetch(`${API_BASE_URL}/system/live-operations/evidence?limit=${limit}`, { cache: "no-store" });
  return handleResponse<Record<string, unknown>>(response);
}

export async function getLivePilot(): Promise<LivePilot> {
  const response = await authenticatedFetch(`${API_BASE_URL}/system/live-pilot`, { cache: "no-store" });
  return handleResponse<LivePilot>(response);
}

export async function activateLivePilot(body: {
  symbols: string[];
  max_notional: string;
  max_order_notional: string;
  starts_at: string;
  expires_at: string;
  observation_window_sessions: number;
  rollback_target: string;
  model_run_id: string;
  paper_expectations: Record<string, unknown>;
  checklist: Record<string, unknown>;
  secondary_approval_actor: string;
  reason: string;
}): Promise<LivePilot> {
  return postJson<LivePilot>("/system/live-pilot/activate", body);
}

export async function promoteLivePilot(body: { secondary_approval_actor: string; reason: string }): Promise<LivePilot> {
  return postJson<LivePilot>("/system/live-pilot/promote", body);
}

export async function stopLivePilot(reason: string): Promise<LivePilot> {
  return postJson<LivePilot>("/system/live-pilot/stop", { reason });
}

export async function rollbackLivePilot(reason: string, secondary_approval_actor?: string): Promise<LivePilot> {
  return postJson<LivePilot>("/system/live-pilot/rollback", { reason, secondary_approval_actor });
}

export async function reviewLivePilot(reason: string): Promise<LivePilot> {
  return postJson<LivePilot>("/system/live-pilot/review", { reason });
}

export async function getStockMonitoring(): Promise<StockMonitoringSnapshot> {
  const response = await authenticatedFetch(`${API_BASE_URL}/system/stock-monitoring`, { cache: "no-store" });
  return handleResponse<StockMonitoringSnapshot>(response);
}

export async function getOperationalHardening(): Promise<OperationalHardeningReport> {
  const response = await authenticatedFetch(`${API_BASE_URL}/system/operational-hardening`, { cache: "no-store" });
  return handleResponse<OperationalHardeningReport>(response);
}

export async function runOperationalHardening(): Promise<OperationalHardeningReport> {
  return postJson<OperationalHardeningReport>("/system/operational-hardening/run", {});
}

export async function runStockMonitoring(): Promise<StockMonitoringSnapshot> {
  return postJson<StockMonitoringSnapshot>("/system/stock-monitoring/run", {});
}

export async function getStockLearningCycles(limit = 25): Promise<StockLearningCycle[]> {
  const response = await authenticatedFetch(`${API_BASE_URL}/stock/learning-cycles?limit=${limit}`, { cache: "no-store" });
  return handleResponse<StockLearningCycle[]>(response);
}

export type StockLearningScheduleControl = {
  paused: boolean;
  pause_reason: string | null;
  updated_by: string;
  updated_at: string | null;
  paper_only: boolean;
  live_authorized: boolean;
  recovery_independent: boolean;
};

export async function getStockLearningScheduleControl(): Promise<StockLearningScheduleControl> {
  const response = await authenticatedFetch(`${API_BASE_URL}/stock/learning-cycles/schedule-control`, { cache: "no-store" });
  return handleResponse<StockLearningScheduleControl>(response);
}

export async function updateStockLearningScheduleControl(
  action: "pause" | "resume",
  reason: string,
): Promise<StockLearningScheduleControl> {
  return postJson<StockLearningScheduleControl>("/stock/learning-cycles/schedule-control", { action, reason });
}
export async function getTradeCandidates(limit = 12, refresh = false): Promise<TradeCandidateResponse> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (refresh) {
    params.set("refresh", "true");
  }
  const response = await authenticatedFetch(`${API_BASE_URL}/trade-candidates?${params.toString()}`, { cache: "no-store" });
  return handleResponse<TradeCandidateResponse>(response);
}

export async function getTradeCandidateEvidence(symbol: string, strategy: string): Promise<TradeCandidateEvidence> {
  const params = new URLSearchParams({ symbol, strategy });
  const response = await authenticatedFetch(`${API_BASE_URL}/trade-candidates/evidence?${params.toString()}`, { cache: "no-store" });
  return handleResponse<TradeCandidateEvidence>(response);
}

export async function getCandidateDecisionJournal(limit = 10, symbol?: string, strategy?: string): Promise<CandidateDecisionJournalEntry[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (symbol) {
    params.set("symbol", symbol);
  }
  if (strategy) {
    params.set("strategy", strategy);
  }
  const response = await authenticatedFetch(`${API_BASE_URL}/trade-candidates/decision-journal?${params.toString()}`, { cache: "no-store" });
  return handleResponse<CandidateDecisionJournalEntry[]>(response);
}

export async function createCandidateDecisionJournalEntry(symbol: string, strategy: string, decision: "approve" | "reject" | "skip" | "review" | "activate" | "candidate", status: string, reason: string): Promise<CandidateDecisionJournalEntry> {
  return postJson<CandidateDecisionJournalEntry>("/trade-candidates/decision-journal", { symbol, strategy, decision, status, reason });
}

export async function getCandidateDecisionScorecard(limit = 50): Promise<CandidateDecisionScorecard> {
  const response = await authenticatedFetch(`${API_BASE_URL}/trade-candidates/decision-scorecard?limit=${limit}`, { cache: "no-store" });
  return handleResponse<CandidateDecisionScorecard>(response);
}

export async function getMemoryReplay(limit = 50, topK = 3): Promise<MemoryReplayResponse> {
  const response = await authenticatedFetch(`${API_BASE_URL}/trade-candidates/memory-replay?limit=${limit}&top_k=${topK}`, { cache: "no-store" });
  return handleResponse<MemoryReplayResponse>(response);
}

export async function startTradeCandidateRefreshJob(trigger = "manual", context: Record<string, unknown> = {}, limit = 50): Promise<ScannerRefreshJob> {
  return postJson<ScannerRefreshJob>("/trade-candidates/refresh-jobs", { trigger, context, limit });
}

export async function getLatestTradeCandidateRefreshJob(): Promise<ScannerRefreshJob | null> {
  const response = await authenticatedFetch(`${API_BASE_URL}/trade-candidates/refresh-jobs/latest`, { cache: "no-store" });
  return handleResponse<ScannerRefreshJob | null>(response);
}

export async function getTradeScorecard(limit = 50): Promise<TradeScorecardResponse> {
  const response = await authenticatedFetch(`${API_BASE_URL}/trade-scorecard?limit=${limit}`, { cache: "no-store" });
  return handleResponse<TradeScorecardResponse>(response);
}

export async function getOpportunityRadar(limit = 8, refreshNews = false, newsProvider: "auto" | "yfinance" | "nasdaq_rss" | "mock" = "auto"): Promise<CompanyOpportunityRadar> {
  const params = new URLSearchParams({ limit: String(limit), refresh_news: String(refreshNews), news_provider: newsProvider });
  const response = await authenticatedFetch(`${API_BASE_URL}/opportunity-radar?${params.toString()}`, { cache: "no-store" });
  return handleResponse<CompanyOpportunityRadar>(response);
}

export async function reviewCandidateActivation(symbol: string, strategy: string, decision: "activate" | "candidate" | "reject", reason: string): Promise<CandidateActivationResponse> {
  return postJson<CandidateActivationResponse>("/trade-candidates/activation-review", { symbol, strategy, decision, reason });
}

export async function acknowledgeNotification(notificationId: number): Promise<NotificationItem> {
  return postJson<NotificationItem>(`/notifications/${notificationId}/acknowledge`, {});
}

export async function resolveNotification(notificationId: number): Promise<NotificationItem> {
  return postJson<NotificationItem>(`/notifications/${notificationId}/resolve`, {});
}

export async function runAndPersistModel(symbol: string): Promise<PersistedModelRunResponse> {
  return postJson<PersistedModelRunResponse>("/models/run", { symbol });
}

export async function scoreRealizedModelPredictions(symbol: string): Promise<ModelRealizationScoreResponse> {
  return postJson<ModelRealizationScoreResponse>("/models/score-realized", { symbol });
}

export async function getModelPerformance(symbol = "SPY", limit = 50): Promise<ModelPerformanceResponse> {
  const response = await authenticatedFetch(`${API_BASE_URL}/models/performance?symbol=${encodeURIComponent(symbol)}&limit=${limit}`, { cache: "no-store" });
  return handleResponse<ModelPerformanceResponse>(response);
}

export async function runStrategyExperiments(symbol: string, strategy: string, applyPromotions: boolean): Promise<StrategyExperimentRunResponse> {
  return postJson<StrategyExperimentRunResponse>("/experiments/run", { symbol, strategy, max_candidates: 3, apply_promotions: applyPromotions });
}

export async function getStrategyExperiments(symbol = "SPY", strategy = "moving_average_crossover", limit = 25): Promise<StrategyExperiment[]> {
  const response = await authenticatedFetch(`${API_BASE_URL}/experiments?symbol=${encodeURIComponent(symbol)}&strategy=${encodeURIComponent(strategy)}&limit=${limit}`, { cache: "no-store" });
  return handleResponse<StrategyExperiment[]>(response);
}

export async function detectMarketRegime(symbol = "SPY"): Promise<MarketRegime> {
  return postJson<MarketRegime>("/market-regimes/detect", { symbol });
}

export async function getMarketRegimes(limit = 20): Promise<MarketRegime[]> {
  const response = await authenticatedFetch(`${API_BASE_URL}/market-regimes?limit=${limit}`, { cache: "no-store" });
  return handleResponse<MarketRegime[]>(response);
}

export async function importNews(symbol = "SPY", provider: "auto" | "yfinance" | "nasdaq_rss" | "mock" = "auto"): Promise<NewsImportResponse> {
  return postJson<NewsImportResponse>("/news/import", { symbol, provider });
}

export async function getNewsSummary(symbol = "SPY", limit = 10): Promise<NewsSentimentSummary> {
  const response = await authenticatedFetch(`${API_BASE_URL}/news/${encodeURIComponent(symbol)}/summary?limit=${limit}`, { cache: "no-store" });
  return handleResponse<NewsSentimentSummary>(response);
}

export async function getNewsArticles(symbol = "SPY", limit = 10): Promise<NewsArticle[]> {
  const response = await authenticatedFetch(`${API_BASE_URL}/news?symbol=${encodeURIComponent(symbol)}&limit=${limit}`, { cache: "no-store" });
  return handleResponse<NewsArticle[]>(response);
}

export async function importEconomicData(): Promise<EconomicImportResponse> {
  return postJson<EconomicImportResponse>("/economic/import", {});
}

export async function getMacroContext(): Promise<MacroContextSummary> {
  const response = await authenticatedFetch(`${API_BASE_URL}/economic/context`, { cache: "no-store" });
  return handleResponse<MacroContextSummary>(response);
}

export async function getEconomicIndicators(limit = 50): Promise<EconomicIndicator[]> {
  const response = await authenticatedFetch(`${API_BASE_URL}/economic/indicators?limit=${limit}`, { cache: "no-store" });
  return handleResponse<EconomicIndicator[]>(response);
}

export async function getBrokerStatus(): Promise<BrokerStatus> {
  const response = await authenticatedFetch(`${API_BASE_URL}/broker/status`, { cache: "no-store" });
  return handleResponse<BrokerStatus>(response);
}

export async function enableKillSwitch(reason = "Manual kill switch from control room."): Promise<SafetyControlResponse> {
  return postJson<SafetyControlResponse>("/safety/kill-switch/enable", { reason });
}

export async function disableKillSwitch(reason = "Manual kill switch reset from control room."): Promise<SafetyControlResponse> {
  return postJson<SafetyControlResponse>("/safety/kill-switch/disable", { reason });
}

export async function pauseStrategies(reason = "Manual pause from control room."): Promise<SafetyControlResponse> {
  return postJson<SafetyControlResponse>("/safety/strategies/pause", { reason });
}

export async function resumeStrategies(reason = "Manual resume from control room."): Promise<SafetyControlResponse> {
  return postJson<SafetyControlResponse>("/safety/strategies/resume", { reason });
}

export async function getRiskSettings(): Promise<RiskRule> {
  const response = await authenticatedFetch(`${API_BASE_URL}/risk/settings`, { cache: "no-store" });
  return handleResponse<RiskRule>(response);
}

export async function updateRiskSettings(settings: RiskSettingsUpdate): Promise<RiskRule> {
  return patchJson<RiskRule>("/risk/settings", settings);
}

const STOCK_TRAINING_PATH = "/stock/training";

export type StockTrainingReport = {
  run_id?: string | null;
  job_id?: string | number | null;
  status: StockTrainingJobStatus;
  trigger?: "manual" | "scheduled" | string;
  dataset_snapshot?: StockDatasetSnapshot | null;
  snapshot?: StockDatasetSnapshot | null;
  model?: StockModelArtifact | null;
  report_hash?: string | null;
  hash?: string | null;
  provenance?: Record<string, unknown> | null;
  validation?: StockValidationSummary | null;
  holdout?: StockValidationSummary | null;
  comparisons?: StockTrainingComparison[];
  baselines?: StockTrainingComparison[];
  failures?: string[];
  failure_reasons?: string[];
  message?: string | null;
  created_at?: string | null;
  completed_at?: string | null;
  selected_model?: string | null;
  dataset_snapshot_id?: string | null;
  dataset_sha256?: string | null;
  dataset_artifact_sha256?: string | null;
  artifact_path?: string | null;
  provider?: string | null;
  universe?: string[] | null;
  cutoff_date?: string | null;
  feature_config_id?: string | null;
  folds?: number | null;
  embargo_days?: number | null;
  final_holdout_rows?: number | null;
  final_holdout_start?: string | null;
  final_holdout_end?: string | null;
  walkforward_metrics?: Record<string, StockTrainingMetric> | null;
  calibration_metrics?: Record<string, StockTrainingMetric> | null;
  final_holdout_metrics?: StockTrainingMetric | null;
  final_holdout_baseline?: StockTrainingMetric | null;
  versions?: Record<string, string> | null;
  code_sha256?: string | null;
  assumptions?: Record<string, unknown> | null;
  eligible_for_binding?: boolean;
  binding_blockers?: string[];
};

export type StockModelBinding = {
  id?: string | number | null;
  binding_id?: string | number | null;
  model_run_id?: string | null;
  model_id?: string | null;
  model_version?: string | null;
  dataset_snapshot_id?: string | null;
  snapshot_id?: string | null;
  report_hash?: string | null;
  binding_sha256?: string | null;
  binding_hash?: string | null;
  purpose?: string | null;
  paper_only: boolean;
  live_authorized?: boolean;
  frozen?: boolean;
  active?: boolean;
  integrity_valid?: boolean;
  integrity_error?: string | null;
  bound_at?: string | null;
  created_at?: string | null;
  bound_by?: string | null;
  confirmation?: string | null;
  reason?: string | null;
  message?: string | null;
  lifecycle_state?: StockModelLifecycleState | null;
  active_binding_id?: string | number | null;
};

export type StockModelLifecycleState =
  | "challenger"
  | "eligible"
  | "paper_canary"
  | "champion"
  | "demoted"
  | "retired";

export type StockModelLifecycleEvent = {
  id: number;
  binding_id?: number | null;
  from_state?: StockModelLifecycleState | null;
  to_state: StockModelLifecycleState;
  action: string;
  actor: string;
  reason: string;
  event_sha256: string;
  created_at: string;
};

export type StockModelLifecycle = {
  model_id: string;
  lifecycle_state: StockModelLifecycleState;
  active_binding_id?: number | null;
  events: StockModelLifecycleEvent[];
  paper_only: boolean;
  live_authorized: boolean;
};

export type StockModelLifecycleAction =
  | "mark_eligible"
  | "start_canary"
  | "promote"
  | "demote"
  | "retire";

export type StockDatasetSnapshot = {
  snapshot_id: string;
  version?: string | null;
  status?: "verified" | "invalid" | "pending" | string;
  verified: boolean;
  provider: string;
  provider_version?: string | null;
  source?: string | null;
  cutoff_at: string;
  universe: string[];
  features: string[];
  label_definition?: string | null;
  rows?: number | null;
  hash?: string | null;
  sha256?: string | null;
  provenance?: Record<string, unknown> | null;
  created_at?: string | null;
  captured_at?: string | null;
  binding_eligible?: boolean;
  binding_eligibility_reason?: string | null;
  adjustments_point_in_time?: boolean;
};

export type StockModelArtifact = {
  model_id?: string | null;
  model_version: string;
  status?: "challenger" | "frozen" | "failed" | "candidate" | string;
  hash?: string | null;
  sha256?: string | null;
  artifact_hash?: string | null;
  created_at?: string | null;
  scheduled?: boolean;
  eligible_for_binding?: boolean;
  binding_blockers?: string[];
  temporal_caveat?: string | null;
};

export type BindStockModelRequest = {
  model_id: string;
  snapshot_id: string;
  confirmation: "PAPER_ONLY_FROZEN_BINDING";
  purpose: string;
  reason: string;
};

export type StockTrainingJobDetail = StockTrainingJob & {
  report?: StockTrainingReport | null;
  snapshot?: StockDatasetSnapshot | null;
  model?: StockModelArtifact | null;
};

export type StockTrainingComparison = {
  name: string;
  phase?: "purged_walkforward" | "calibration" | "final_untouched_holdout" | string;
  kind?: "model" | "baseline" | string;
  calibration?: number | null;
  brier_score?: number | null;
  log_loss?: number | null;
  ece?: number | null;
  gross_return?: number | null;
  net_return?: number | null;
  cost_adjusted_return?: number | null;
  max_drawdown?: number | null;
  drawdown?: number | null;
  count?: number | null;
  sample_count?: number | null;
  losing_count?: number | null;
  losses?: number | null;
  metrics?: StockTrainingMetric;
};

function asStockJobsPage(payload: StockTrainingJobsPage | StockTrainingJob[]): StockTrainingJobsPage {
  if (Array.isArray(payload)) {
    return { items: payload, total: payload.length };
  }
  return {
    items: Array.isArray(payload?.items) ? payload.items : [],
    total: typeof payload?.total === "number" ? payload.total : payload?.items?.length ?? 0,
    limit: payload?.limit,
    offset: payload?.offset,
  };
}

export async function startStockTraining(request: StartStockTrainingRequest): Promise<StockTrainingJob> {
  return postJson<StockTrainingJob>(`${STOCK_TRAINING_PATH}/jobs`, {
    ...request,
  });
}

export async function cancelStockTraining(jobId: string | number): Promise<StockTrainingJob> {
  return postJson<StockTrainingJob>(`${STOCK_TRAINING_PATH}/jobs/${encodeURIComponent(String(jobId))}/cancel`, {});
}

export type StockValidationSummary = {
  status?: "passed" | "failed" | "warning" | string;
  phase?: string;
  selected_model?: string | null;
  chosen_model?: string | null;
  chosen_model_metrics?: StockTrainingComparison | null;
  baseline_metrics?: StockTrainingComparison | null;
  walkforward_comparisons?: StockTrainingComparison[];
  calibration_comparisons?: StockTrainingComparison[];
  folds?: number | null;
  purged_rows?: number | null;
  embargo_rows?: number | null;
  train_rows?: number | null;
  validation_rows?: number | null;
  holdout_rows?: number | null;
  holdout_start?: string | null;
  holdout_end?: string | null;
  repeated_holdout_uses?: number | null;
  failures?: string[];
  failure_reasons?: string[];
  [key: string]: unknown;
};

export async function getStockTrainingReport(runId: string | number): Promise<StockTrainingReport> {
  const response = await authenticatedFetch(`${API_BASE_URL}${STOCK_TRAINING_PATH}/runs/${encodeURIComponent(String(runId))}/report`, { cache: "no-store" });
  return handleResponse<StockTrainingReport>(response);
}

export async function getActiveStockModelBinding(): Promise<StockModelBinding | null> {
  const response = await authenticatedFetch(`${API_BASE_URL}${STOCK_TRAINING_PATH}/binding`, { cache: "no-store" });
  return handleResponse<StockModelBinding | null>(response);
}

export async function bindStockModel(request: BindStockModelRequest): Promise<StockModelBinding> {
  return postJson<StockModelBinding>(`${STOCK_TRAINING_PATH}/binding`, request);
}

export async function getStockModelLifecycle(modelId: string): Promise<StockModelLifecycle> {
  const response = await authenticatedFetch(
    `${API_BASE_URL}${STOCK_TRAINING_PATH}/models/${encodeURIComponent(modelId)}/lifecycle`,
    { cache: "no-store" },
  );
  return handleResponse<StockModelLifecycle>(response);
}

export async function changeStockModelLifecycle(
  modelId: string,
  action: StockModelLifecycleAction,
  reason: string,
): Promise<StockModelLifecycle & { lifecycle_event_id?: number | null }> {
  return postJson<StockModelLifecycle & { lifecycle_event_id?: number | null }>(
    `${STOCK_TRAINING_PATH}/models/${encodeURIComponent(modelId)}/lifecycle`,
    { action, reason },
  );
}

export async function getStockTrainingJobs(limit = 50, offset = 0): Promise<StockTrainingJobsPage> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  const response = await authenticatedFetch(`${API_BASE_URL}${STOCK_TRAINING_PATH}/jobs?${params.toString()}`, { cache: "no-store" });
  const payload = await handleResponse<StockTrainingJobsPage | StockTrainingJob[]>(response);
  return asStockJobsPage(payload);
}

export type StockTrainingMetric = {
  calibration?: number | null;
  brier_score?: number | null;
  log_loss?: number | null;
  ece?: number | null;
  return?: number | null;
  gross_return?: number | null;
  net_return?: number | null;
  cost_adjusted_return?: number | null;
  max_drawdown?: number | null;
  drawdown?: number | null;
  count?: number | null;
  sample_count?: number | null;
  losing_count?: number | null;
  losses?: number | null;
  cost_aware_nonoverlapping_returns?: {
    sample_count?: number | null;
    wins?: number | null;
    losses?: number | null;
    total_return?: number | null;
    mean_return?: number | null;
    max_drawdown?: number | null;
    [key: string]: number | boolean | string | null | undefined;
  } | null;
  [key: string]: unknown;
};

export type StockTrainingJobsPage = {
  items: StockTrainingJob[];
  total: number;
  limit?: number;
  offset?: number;
};

export type StockTrainingJob = {
  id: string | number;
  job_id?: string;
  run_id?: string | null;
  result_run_id?: string | null;
  status: StockTrainingJobStatus;
  trigger?: "manual" | "scheduled" | string;
  requested_by?: string | null;
  request?: {
    symbols?: string[];
    horizon_bars?: number;
    seed?: number;
    paper_only?: boolean;
    [key: string]: unknown;
  } | null;
  dataset_snapshot_id?: string | null;
  holdout_reservation_id?: number | null;
  attempts?: number;
  queue_error?: string | null;
  failure_code?: string | null;
  failure_detail?: string | null;
  paper_only?: boolean;
  live_authorized?: boolean;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  cancelled_at?: string | null;
  snapshot_id?: string | null;
  model_id?: string | null;
  model_version?: string | null;
  message?: string | null;
  error?: string | null;
  failure_reason?: string | null;
  report_available?: boolean;
};

export async function getStockTrainingJob(jobId: string | number): Promise<StockTrainingJobDetail> {
  const response = await authenticatedFetch(`${API_BASE_URL}${STOCK_TRAINING_PATH}/jobs/${encodeURIComponent(String(jobId))}`, { cache: "no-store" });
  return handleResponse<StockTrainingJobDetail>(response);
}

export type StartStockTrainingRequest = {
  symbols: string[];
  cutoff_at: string;
  horizon_bars: number;
  provider?: "yfinance" | "yahoo_chart";
  trigger?: "manual" | "scheduled";
  seed?: number;
};

export type ForwardTrialBindingEligible = {
  binding_id: number;
  model_run_id: string | null;
  snapshot_id: string | null;
  eligible: boolean;
  reason: string | null;
  paper_only: boolean;
  live_authorized: boolean;
};

export type ForwardTrial = {
  id: string;
  status: "approved" | "blocked" | "paused" | "running" | "stopped" | "completed" | string;
  binding_id: number;
  source_cycle_id?: string | null;
  policy: Record<string, unknown>;
  lineage: Record<string, unknown>;
  blocked_reason: string | null;
  pause_reason: string | null;
  started_at: string | null;
  stopped_at: string | null;
  strategy_name?: string;
  symbol?: string;
  model_hash?: string;
  model_cutoff?: string;
  universe?: string;
  paper_only?: boolean;
  live_disabled?: boolean;
};

export type ForwardTrialPreflight = {
  status: string;
  ready: boolean;
  checked_at: string;
  regular_session: boolean;
  symbols: Array<{
    symbol: string;
    status: string;
    failure_class?: string | null;
    entitlement_state?: string;
    exchange_timestamp: string | null;
    ingestion_timestamp: string | null;
    latency_seconds: number | null;
    missing_intervals: string[];
    unavailable_reason: string | null;
  }>;
  paper_ledger: { status: string; reason: string | null };
  next_regular_session_open: string | null;
  reason: string | null;
  paper_only: boolean;
  live_authorized: boolean;
};
export type ForwardTrialDecision = {
  id: number;
  symbol: string;
  bar_timestamp: string;
  action: string;
  qualifying: boolean;
  rejection_reason: string | null;
  lineage: Record<string, unknown>;
  order_id: number | null;
};

export type ForwardTrialDecisionHistory = {
  trial_id: string;
  items: ForwardTrialDecision[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
};

export type ForwardTrialMetricHistoryItem = {
  as_of: string;
  classification: string;
  payload: {
    observed_sessions?: number;
    expected_observations?: number;
    observed_observations?: number;
    decision_coverage?: string | null;
    closed_trades?: number;
    provisional_gross_pnl?: string | null;
    gross_pnl?: string | null;
    net_pnl?: string | null;
    expectancy?: string | null;
    win_rate?: string | null;
    max_drawdown?: string | null;
    account_drawdown?: string | null;
    benchmark_buy_hold?: string | null;
    costs_known?: boolean;
    accuracy?: StockPointInTimeAccuracyEvidence;
    accuracy_report_id?: number | null;
    accuracy_report_hash?: string | null;
    [key: string]: unknown;
  };
};

export type StockPointInTimeAccuracyEvidence = {
  version?: number;
  as_of?: string;
  classification: "sufficient" | "insufficient" | "degraded" | "blocked" | "unknown" | string;
  reason?: string | null;
  contract?: {
    version?: string;
    population?: string;
    feature_cutoff?: string;
    data_vintage?: string;
    labels?: string;
    window_limit?: number;
    holdout_reuse?: string;
  };
  window?: {
    sample_count?: number;
    prediction_count?: number;
    unknown_count?: number;
    bounded?: boolean;
  };
  accuracy?: {
    value?: number | null;
    confidence_interval?: { lower: number; upper: number; level: number } | null;
  };
  calibration?: {
    brier_score?: number | null;
    log_loss?: number | null;
    expected_calibration_error?: number | null;
  };
  baseline?: {
    kind?: string;
    prevalence?: number | null;
    brier_score?: number | null;
    log_loss?: number | null;
  };
  coverage?: {
    value?: number | null;
    resolved?: number;
    predictions?: number;
    confidence_interval?: { lower: number; upper: number; level: number } | null;
  };
  cost_aware?: {
    sample_count?: number;
    total_return?: number | null;
    mean_return?: number | null;
    max_drawdown?: number | null;
    costs_known?: boolean;
  };
  data_health?: {
    status?: string;
    unknown_labels?: number;
    restated_labels?: number;
    blocked_labels?: number;
    reasons?: string[];
  };
  drift?: {
    status?: string;
    score?: number | null;
    probability_shift?: number | null;
    threshold?: number | null;
    sample_count?: number;
    reason?: string | null;
  };
  limitations?: string[];
  [key: string]: unknown;
};

export type StockPointInTimeAccuracyReport = {
  id: number;
  trial_id: string;
  as_of: string;
  classification: string;
  report_hash: string;
  lineage: Record<string, unknown>;
  evidence: StockPointInTimeAccuracyEvidence;
  paper_only: boolean;
  live_authorized: false;
};

export type ForwardTrialMetricHistory = {
  trial_id: string;
  items: ForwardTrialMetricHistoryItem[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
};

/**
 * Evidence is deliberately separate from elapsed trial metrics.  A session
 * can have elapsed while its decisions are missing, rejected, stale, or
 * otherwise not attributable to this trial.  Keep the source identifiers
 * opaque so the UI can link to trial-owned evidence without exposing broker
 * payloads.
 */
export type ForwardTrialEvidenceSource = {
  source_id?: string | null;
  kind?: string | null;
  label?: string | null;
  identifier?: string | null;
};

export type ForwardTrialEvidenceSymbol = {
  symbol: string;
  status?: "evidenced" | "partial" | "missing" | "rejected" | "unknown" | string;
  expected_decisions: number;
  observed_decisions: number;
  verified_decisions?: number;
  duplicate_exclusions?: number;
  rejected_decisions: number;
  missing_decisions: number;
  missing_reasons: string[];
  rejected_reasons: string[];
  execution_sources: ForwardTrialEvidenceSource[];
};

export type ForwardTrialEvidenceSession = {
  session_id: string;
  session_date?: string | null;
  status: "evidenced" | "partial" | "missing" | "rejected" | "unknown" | string;
  expected_decisions: number;
  observed_decisions: number;
  verified_decisions?: number;
  duplicate_exclusions?: number;
  rejected_decisions: number;
  missing_decisions: number;
  missing_reasons: string[];
  rejected_reasons: string[];
  symbols: ForwardTrialEvidenceSymbol[];
  execution_sources: ForwardTrialEvidenceSource[];
};

export type ForwardTrialEvidenceSummary = {
  elapsed_sessions: number;
  evidenced_sessions: number;
  expected_decisions: number;
  observed_decisions: number;
  verified_decisions?: number;
  duplicate_exclusions?: number;
  rejected_decisions: number;
  missing_decisions: number;
  decision_coverage: string | null;
  classification: "accumulating" | "insufficient" | "failing" | "passing" | "unknown" | string;
  costs_known: boolean | null;
  historical_evidence_status?: "verified" | "unknown" | "unavailable" | string;
  historical_evidence_reason?: string | null;
};

export type ForwardTrialEvidence = {
  trial_id: string;
  as_of: string | null;
  summary: ForwardTrialEvidenceSummary;
  sessions: ForwardTrialEvidenceSession[];
  lineage?: Record<string, unknown>;
  policy?: Record<string, unknown>;
  source_links?: ForwardTrialEvidenceSource[];
  execution_sources?: ForwardTrialEvidenceSource[];
};

export type ForwardTrialReport = {
  id: string | number;
  trial_id: string;
  version: number | string;
  created_at: string;
  as_of: string | null;
  classification: "accumulating" | "insufficient" | "failing" | "passing" | "unknown" | string;
  report_hash: string;
  digest?: string;
  lineage: Record<string, unknown>;
  policy: Record<string, unknown>;
  summary: ForwardTrialEvidenceSummary;
  sessions: ForwardTrialEvidenceSession[];
  readiness_gates: Record<string, PromotionReadinessGate>;
  reproducibility?: {
    trial_id?: string;
    model_id?: string | null;
    model_hash?: string | null;
    policy_version?: string | null;
    source_ids?: string[];
    as_of?: string | null;
  };
  source_ids?: string[];
  source_links?: ForwardTrialEvidenceSource[];
  execution_sources?: ForwardTrialEvidenceSource[];
  paper_only: boolean;
  live_authorized: false;
  promotion_authorized: false;
  resume_authorized?: false;
  download_url?: string | null;
};

export type ForwardTrialReportHistory = {
  trial_id: string;
  items: ForwardTrialReport[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
};

export type ForwardTrialSessionEvidencePage = {
  trial_id: string;
  report_id: number;
  as_of: string | null;
  session_evidence: NonNullable<NonNullable<PromotionReadinessReport["evidence"]>["session_evidence"]>;
  items: Array<Record<string, unknown>>;
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
};

export type PromotionReadinessGate = {
  status: "pass" | "fail" | "unknown" | string;
  value?: string | number | null;
  required?: string | number | null;
  reason?: string | null;
};

export type PromotionReadinessReport = {
  id: number;
  version?: number | null;
  trial_id: string;
  source_metric_id: number | null;
  as_of?: string | null;
  report_hash: string;
  decision: "pass" | "fail" | "unknown" | string;
  gates: Record<string, PromotionReadinessGate>;
  lineage: Record<string, unknown>;
  policy: Record<string, unknown>;
  summary?: ForwardTrialEvidenceSummary;
  session_evidence_available?: boolean;
  evidence?: {
    version?: number;
    trial_id?: string;
    source_metric_id?: number | null;
    source_metric_as_of?: string | null;
    as_of?: string | null;
    decision?: string;
    gates?: Record<string, PromotionReadinessGate>;
    lineage?: Record<string, unknown>;
    policy?: Record<string, unknown>;
    aggregate_metrics?: Record<string, unknown> | null;
    session_evidence?: {
      version?: number;
      as_of?: string | null;
      window?: {
        started_at?: string | null;
        regular_sessions_required?: number;
        elapsed_regular_sessions?: number;
        complete?: boolean;
        session_dates?: string[];
      };
      sessions?: Array<Record<string, unknown>>;
      aggregates?: Record<string, unknown>;
      source_lineage?: Record<string, unknown>;
    } | null;
    paper_only?: boolean;
    live_authorized?: boolean;
    promotion_authorized?: false;
  } | null;
  paper_only: boolean;
  live_authorized: boolean;
  created_at: string;
  promotion_authorized: false;
};

export type PaperGraduationBlocker = {
  category: string;
  key: string;
  status: "blocked";
  reason: string;
  source?: unknown;
};

export type PaperGraduationPackage = {
  id: number;
  trial_id: string;
  cycle_id?: string | null;
  readiness_report_id: number;
  accuracy_report_id?: number | null;
  package_hash: string;
  readiness_report_hash: string;
  soak_report_hash?: string | null;
  decision: "approved" | "rejected";
  blockers: PaperGraduationBlocker[];
  evidence?: Record<string, unknown> | null;
  reviewer_actor: string;
  reviewer_reason: string;
  authorization: Record<string, unknown>;
  paper_only: true;
  live_authorized: false;
  live_orders_allowed: false;
  created_at: string;
};

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : Number.isFinite(Number(value)) ? Number(value) : fallback;
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function reasons(value: unknown): string[] {
  if (Array.isArray(value)) return value.filter((item): item is string => typeof item === "string");
  const text = asString(value);
  return text ? [text] : [];
}

function executionSources(executionValue: unknown): ForwardTrialEvidenceSource[] {
  const execution = asRecord(executionValue);
  if (execution.status === "not_applicable") return [];
  const sources: ForwardTrialEvidenceSource[] = [];
  for (const [key, value] of Object.entries(execution)) {
    if (key === "status" || value == null || value === "") continue;
    const values = Array.isArray(value) ? value : [value];
    values.forEach((item) => {
      if (typeof item === "string" || typeof item === "number") {
        sources.push({ source_id: String(item), kind: key, label: `${key.replaceAll("_", " ")} ${item}`, identifier: String(item) });
      }
    });
  }
  return sources;
}

function evidenceClassification(report: PromotionReadinessReport, aggregateMetrics: Record<string, unknown>): string {
  const metricClassification = asString(aggregateMetrics.classification);
  if (metricClassification) return metricClassification;
  if (report.decision === "pass") return "passing";
  if (report.decision === "fail") return "failing";
  return "insufficient";
}

/**
 * Convert the immutable readiness report's nested session evidence into the
 * stable UI contract. The backend intentionally keeps execution references as
 * identifiers only; no broker payload is ever sent to the browser.
 */
export function forwardTrialEvidenceFromReadiness(report: PromotionReadinessReport): ForwardTrialEvidence | null {
  const nested = report.evidence?.session_evidence;
  if (!nested) return null;
  const nestedRecord = nested as Record<string, unknown>;
  const window = asRecord(nestedRecord.window);
  const aggregate = asRecord(nestedRecord.aggregates);
  const aggregateMetrics = asRecord(report.evidence?.aggregate_metrics);
  const rawSessions = Array.isArray(nestedRecord.sessions) ? nestedRecord.sessions : [];
  const sessions: ForwardTrialEvidenceSession[] = rawSessions.map((rawValue, sessionIndex) => {
    const raw = asRecord(rawValue);
    const rawSymbols = Array.isArray(raw.symbols) ? raw.symbols : [];
    const symbols: ForwardTrialEvidenceSymbol[] = rawSymbols.map((symbolValue) => {
      const symbol = asRecord(symbolValue);
      const execution = asRecord(symbol.execution);
      const expected = asNumber(symbol.expected_decisions, 1);
      const observed = asNumber(symbol.observed_decisions);
      const rejected = symbol.status === "rejected" || asString(symbol.rejected_reason) ? 1 : 0;
      const missing = asString(symbol.missing_reason) ? 1 : Math.max(0, expected - observed);
      return {
        symbol: asString(symbol.symbol) ?? `symbol-${sessionIndex}`,
        status: asString(symbol.status) ?? "unknown",
        expected_decisions: expected,
        observed_decisions: observed,
        verified_decisions: asNumber(symbol.verified_decisions),
        duplicate_exclusions: asNumber(symbol.duplicate_exclusions),
        rejected_decisions: rejected,
        missing_decisions: missing,
        missing_reasons: reasons(symbol.missing_reason),
        rejected_reasons: reasons(symbol.rejected_reason),
        execution_sources: executionSources(execution),
      };
    });
    const expected = symbols.reduce((total, symbol) => total + symbol.expected_decisions, 0);
    const observed = symbols.reduce((total, symbol) => total + symbol.observed_decisions, 0);
    const verified = symbols.reduce((total, symbol) => total + (symbol.verified_decisions ?? 0), 0);
    const rejected = symbols.reduce((total, symbol) => total + symbol.rejected_decisions, 0);
    const missing = symbols.reduce((total, symbol) => total + symbol.missing_decisions, 0);
    const complete = expected > 0 && verified >= expected;
    const status = complete ? "evidenced" : rejected > 0 ? "rejected" : missing > 0 ? "missing" : "partial";
    return {
      session_id: asString(raw.session_date) ?? `session-${sessionIndex + 1}`,
      session_date: asString(raw.session_date),
      status,
      expected_decisions: expected,
      observed_decisions: observed,
      verified_decisions: verified,
      duplicate_exclusions: symbols.reduce((total, symbol) => total + (symbol.duplicate_exclusions ?? 0), 0),
      rejected_decisions: rejected,
      missing_decisions: missing,
      missing_reasons: symbols.flatMap((symbol) => symbol.missing_reasons),
      rejected_reasons: symbols.flatMap((symbol) => symbol.rejected_reasons),
      symbols,
      execution_sources: symbols.flatMap((symbol) => symbol.execution_sources),
    };
  });
  const costsKnown = typeof aggregateMetrics.costs_known === "boolean" ? aggregateMetrics.costs_known : null;
  const unknownHistorical = asNumber(aggregate.unknown_historical_feed_health);
  const summary: ForwardTrialEvidenceSummary = {
    elapsed_sessions: asNumber(window.elapsed_regular_sessions, sessions.length),
    evidenced_sessions: asNumber(
      aggregate.evidenced_sessions,
      sessions.filter((session) => session.status === "evidenced").length,
    ),
    expected_decisions: asNumber(aggregate.expected_decisions, sessions.reduce((total, session) => total + session.expected_decisions, 0)),
    observed_decisions: asNumber(aggregate.observed_decisions, sessions.reduce((total, session) => total + session.observed_decisions, 0)),
    verified_decisions: asNumber(aggregate.verified_decisions),
    duplicate_exclusions: asNumber(aggregate.duplicate_exclusions),
    rejected_decisions: asNumber(aggregate.rejected_decisions),
    missing_decisions: asNumber(aggregate.missing_decisions),
    decision_coverage: asString(aggregate.decision_coverage),
    classification: evidenceClassification(report, aggregateMetrics),
    costs_known: costsKnown,
    historical_evidence_status: unknownHistorical > 0 || sessions.length === 0 ? "unknown" : "verified",
    historical_evidence_reason: unknownHistorical > 0 || sessions.length === 0 ? "Historical feed or health evidence is unavailable; it was not reconstructed" : null,
  };
  const sourceLineage = asRecord(nestedRecord.source_lineage);
  return {
    trial_id: report.trial_id,
    as_of: asString(nestedRecord.as_of) ?? report.as_of ?? null,
    summary,
    sessions,
    lineage: Object.keys(sourceLineage).length > 0 ? sourceLineage : report.lineage,
    policy: report.policy,
    source_links: Object.entries(sourceLineage).map(([key, value]) => ({ source_id: asString(value), kind: key, label: `${key.replaceAll("_", " ")}: ${String(value ?? "Unknown")}` })),
    execution_sources: sessions.flatMap((session) => session.execution_sources),
  };
}

export function forwardTrialEvidenceFromSessionPage(
  report: PromotionReadinessReport,
  page: ForwardTrialSessionEvidencePage,
): ForwardTrialEvidence | null {
  const evidence = {
    ...(report.evidence ?? {}),
    session_evidence: page.session_evidence,
  };
  return forwardTrialEvidenceFromReadiness({ ...report, evidence });
}

function forwardTrialReportFromReadiness(report: PromotionReadinessReport): ForwardTrialReport {
  const evidence = forwardTrialEvidenceFromReadiness(report);
  const aggregateMetrics = asRecord(report.evidence?.aggregate_metrics);
  const lineageSourceIds = Object.values(report.lineage).flatMap((value) => typeof value === "string" ? [value] : []);
  const executionSourceIds = evidence?.execution_sources?.flatMap((source) => source.source_id ? [source.source_id] : source.identifier ? [source.identifier] : []) ?? [];
  return {
    id: report.id,
    trial_id: report.trial_id,
    version: report.version ?? "unknown",
    created_at: report.created_at,
    as_of: report.as_of ?? report.evidence?.as_of ?? null,
    classification: evidence?.summary.classification ?? evidenceClassification(report, aggregateMetrics),
    report_hash: report.report_hash,
    lineage: report.lineage,
    policy: report.policy,
    summary: evidence?.summary ?? report.summary ?? {
      elapsed_sessions: 0,
      evidenced_sessions: 0,
      expected_decisions: 0,
      observed_decisions: 0,
      rejected_decisions: 0,
      missing_decisions: 0,
      decision_coverage: null,
      classification: evidenceClassification(report, aggregateMetrics),
      costs_known: typeof aggregateMetrics.costs_known === "boolean" ? aggregateMetrics.costs_known : null,
    },
    sessions: evidence?.sessions ?? [],
    readiness_gates: report.gates,
    reproducibility: {
      trial_id: report.trial_id,
      model_id: asString(report.lineage.model_run_id),
      model_hash: asString(report.lineage.model_hash),
      policy_version: asString(report.lineage.policy_version),
      source_ids: [...lineageSourceIds, ...executionSourceIds],
      as_of: report.as_of ?? null,
    },
    source_ids: [...lineageSourceIds, ...executionSourceIds],
    source_links: evidence?.source_links,
    execution_sources: evidence?.execution_sources,
    paper_only: report.paper_only,
    live_authorized: false,
    promotion_authorized: false,
    resume_authorized: false,
  };
}

export async function getForwardTrialsEligibleBindings(): Promise<ForwardTrialBindingEligible[]> {
  const response = await authenticatedFetch(`${API_BASE_URL}/stock/forward-trials/bindings/eligible`, { cache: "no-store" });
  const data = await handleResponse<{items: ForwardTrialBindingEligible[]}>(response);
  return data.items;
}

export async function getForwardTrials(): Promise<ForwardTrial[]> {
  const response = await authenticatedFetch(`${API_BASE_URL}/stock/forward-trials`, { cache: "no-store" });
  const data = await handleResponse<{items: ForwardTrial[]}>(response);
  return data.items;
}

export async function createForwardTrial(bindingId: number): Promise<ForwardTrial> {
  return postJson<ForwardTrial>(`/stock/forward-trials`, { binding_id: bindingId });
}

export async function getForwardTrialDetail(id: string): Promise<ForwardTrial> {
  const response = await authenticatedFetch(`${API_BASE_URL}/stock/forward-trials/${encodeURIComponent(id)}`, { cache: "no-store" });
  return handleResponse<ForwardTrial>(response);
}

export async function getForwardTrialDecisions(
  id: string,
  options: { limit?: number; offset?: number } = {},
): Promise<ForwardTrialDecisionHistory> {
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined) params.set("offset", String(options.offset));
  const query = params.toString();
  const response = await authenticatedFetch(
    `${API_BASE_URL}/stock/forward-trials/${encodeURIComponent(id)}/decisions${query ? `?${query}` : ""}`,
    { cache: "no-store" },
  );
  const data = await handleResponse<{
    items: ForwardTrialDecision[];
    total?: number;
    limit?: number;
    offset?: number;
    has_more?: boolean;
  }>(response);
  const items = data.items;
  return {
    trial_id: id,
    items,
    total: data.total ?? items.length,
    limit: data.limit ?? options.limit ?? items.length,
    offset: data.offset ?? options.offset ?? 0,
    has_more: data.has_more ?? false,
  };
}

export async function getForwardTrialMetrics(
  id: string,
  options: { limit?: number; offset?: number } = {},
): Promise<ForwardTrialMetricHistory> {
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined) params.set("offset", String(options.offset));
  const query = params.toString();
  const response = await authenticatedFetch(
    `${API_BASE_URL}/stock/forward-trials/${encodeURIComponent(id)}/metrics${query ? `?${query}` : ""}`,
    { cache: "no-store" },
  );
  const data = await handleResponse<{
    items: ForwardTrialMetricHistoryItem[];
    total?: number;
    limit?: number;
    offset?: number;
    has_more?: boolean;
  }>(response);
  const items = data.items;
  return {
    trial_id: id,
    items,
    total: data.total ?? items.length,
    limit: data.limit ?? options.limit ?? items.length,
    offset: data.offset ?? options.offset ?? 0,
    has_more: data.has_more ?? false,
  };
}

export async function getForwardTrialAccuracy(id: string): Promise<StockPointInTimeAccuracyReport | null> {
  const response = await authenticatedFetch(
    `${API_BASE_URL}/stock/forward-trials/${encodeURIComponent(id)}/accuracy`,
    { cache: "no-store" },
  );
  return handleResponse<StockPointInTimeAccuracyReport | null>(response);
}

export async function getForwardTrialPreflight(id: string): Promise<ForwardTrialPreflight> {
  const response = await authenticatedFetch(`${API_BASE_URL}/stock/forward-trials/${encodeURIComponent(id)}/preflight`, { cache: "no-store" });
  return handleResponse<ForwardTrialPreflight>(response);
}
export async function getForwardTrialPromotionReadiness(
  id: string,
  options: { includeSessionEvidence?: boolean } = {},
): Promise<PromotionReadinessReport> {
  const params = new URLSearchParams();
  if (options.includeSessionEvidence !== undefined) {
    params.set("include_session_evidence", String(options.includeSessionEvidence));
  }
  const query = params.toString();
  const response = await authenticatedFetch(
    `${API_BASE_URL}/stock/forward-trials/${encodeURIComponent(id)}/promotion-readiness${query ? `?${query}` : ""}`,
    { cache: "no-store" },
  );
  return handleResponse<PromotionReadinessReport>(response);
}

export async function getForwardTrialReportHistory(
  id: string,
  options: { limit?: number; offset?: number } = {},
): Promise<ForwardTrialReportHistory> {
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined) params.set("offset", String(options.offset));
  const query = params.toString();
  const response = await authenticatedFetch(
    `${API_BASE_URL}/stock/forward-trials/${encodeURIComponent(id)}/promotion-readiness/reports${query ? `?${query}` : ""}`,
    { cache: "no-store" },
  );
  const data = await handleResponse<{
    items: PromotionReadinessReport[];
    total?: number;
    limit?: number;
    offset?: number;
    has_more?: boolean;
  }>(response);
  const items = data.items.map(forwardTrialReportFromReadiness);
  return {
    trial_id: id,
    items,
    total: data.total ?? items.length,
    limit: data.limit ?? options.limit ?? items.length,
    offset: data.offset ?? options.offset ?? 0,
    has_more: data.has_more ?? false,
  };
}

export async function getForwardTrialSessionEvidence(
  id: string,
  reportId: string | number,
  options: { limit?: number; offset?: number } = {},
): Promise<ForwardTrialSessionEvidencePage> {
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined) params.set("offset", String(options.offset));
  const query = params.toString();
  const response = await authenticatedFetch(
    `${API_BASE_URL}/stock/forward-trials/${encodeURIComponent(id)}/promotion-readiness/reports/${encodeURIComponent(String(reportId))}/session-evidence${query ? `?${query}` : ""}`,
    { cache: "no-store" },
  );
  return handleResponse<ForwardTrialSessionEvidencePage>(response);
}

export async function getForwardTrialReport(id: string, reportId: string | number): Promise<ForwardTrialReport> {
  const response = await authenticatedFetch(
    `${API_BASE_URL}/stock/forward-trials/${encodeURIComponent(id)}/promotion-readiness/reports/${encodeURIComponent(String(reportId))}`,
    { cache: "no-store" },
  );
  return forwardTrialReportFromReadiness(await handleResponse<PromotionReadinessReport>(response));
}

/**
 * Download an immutable report version.  The endpoint returns the exact
 * server-generated bytes (rather than rebuilding a report in the browser).
 */
export async function downloadForwardTrialReport(id: string, reportId: string | number): Promise<{ blob: Blob; filename: string }> {
  const response = await authenticatedFetch(
    `${API_BASE_URL}/stock/forward-trials/${encodeURIComponent(id)}/promotion-readiness/reports/${encodeURIComponent(String(reportId))}/download`,
    { cache: "no-store" },
  );
  const contentDisposition = response.headers.get("content-disposition") ?? "";
  const filenameMatch = contentDisposition.match(/filename="?([^"]+)"?/i);
  return {
    blob: await response.blob(),
    filename: filenameMatch?.[1] ?? `forward-trial-${id}-report-${reportId}.json`,
  };
}

export async function getPaperGraduationPackages(id: string): Promise<PaperGraduationPackage[]> {
  const response = await authenticatedFetch(
    `${API_BASE_URL}/stock/forward-trials/${encodeURIComponent(id)}/graduation-packages`,
    { cache: "no-store" },
  );
  return (await handleResponse<{ items: PaperGraduationPackage[] }>(response)).items;
}

export async function reviewPaperGraduation(
  id: string,
  readinessReportId: number,
  decision: "approved" | "rejected",
  reason: string,
): Promise<PaperGraduationPackage> {
  return postJson<PaperGraduationPackage>(
    `/stock/forward-trials/${encodeURIComponent(id)}/graduation-packages`,
    { readiness_report_id: readinessReportId, decision, reason },
  );
}

export async function downloadPaperGraduationPackage(id: string, packageId: number): Promise<{ blob: Blob; filename: string }> {
  const response = await authenticatedFetch(
    `${API_BASE_URL}/stock/forward-trials/${encodeURIComponent(id)}/graduation-packages/${packageId}/download`,
    { cache: "no-store" },
  );
  const contentDisposition = response.headers.get("content-disposition") ?? "";
  const filenameMatch = contentDisposition.match(/filename="?([^"]+)"?/i);
  return {
    blob: await response.blob(),
    filename: filenameMatch?.[1] ?? `paper-graduation-${packageId}.json`,
  };
}

export async function startForwardTrial(id: string): Promise<ForwardTrial> {
  return postJson<ForwardTrial>(`/stock/forward-trials/${encodeURIComponent(id)}/start`, {});
}

export async function pauseForwardTrial(id: string, reason: string): Promise<ForwardTrial> {
  return postJson<ForwardTrial>(`/stock/forward-trials/${encodeURIComponent(id)}/pause`, { reason });
}

export async function resumeForwardTrial(id: string): Promise<ForwardTrial> {
  return postJson<ForwardTrial>(`/stock/forward-trials/${encodeURIComponent(id)}/resume`, {});
}

export async function stopForwardTrial(id: string): Promise<ForwardTrial> {
  return postJson<ForwardTrial>(`/stock/forward-trials/${encodeURIComponent(id)}/stop`, {});
}

export async function reviewStockLearningCycle(cycleId: string, reason: string, trialId?: string): Promise<StockLearningCycle> {
  return postJson<StockLearningCycle>(`/stock/learning-cycles/${encodeURIComponent(cycleId)}/review`, {
    reason,
    ...(trialId ? { trial_id: trialId } : {}),
  });
}

export type PaperRunApprovalRecord = {
  id: number;
  environment: "paper";
  execution_provider: string;
  provider_switch: Record<string, string> | null;
  symbols: string[];
  exposure_limits: Record<string, string | number>;
  loss_limits: Record<string, string | number>;
  duration_sessions: number;
  schedule: Record<string, string>;
  stop_conditions: string[];
  stop_authority: string;
  pending_order_treatment: string;
  remaining_position_policy: string;
  approving_actors: string[];
  approval_sha256: string;
  created_at: string;
};

export type PaperRunApproval = {
  status: "missing" | "pass" | "mismatch" | string;
  reason: string | null;
  record: PaperRunApprovalRecord | null;
  expected: {
    environment: "paper";
    execution_provider: string;
    provider_switch: Record<string, string> | null;
    symbols: string[];
    exposure_limits: Record<string, string | number>;
    loss_limits: Record<string, string | number>;
    duration_sessions: number;
    schedule: Record<string, string>;
    stop_conditions: string[];
    stop_authority: string;
    pending_order_treatment: string;
    remaining_position_policy: string;
  };
  paper_only: true;
  live_authorized: false;
};

export type CreatePaperRunApprovalRequest = {
  environment: "paper";
  execution_provider: string;
  provider_switch: Record<string, string> | null;
  symbols: string[];
  exposure_limits: Record<string, string | number>;
  loss_limits: Record<string, string | number>;
  duration_sessions: number;
  schedule: Record<string, string>;
  stop_conditions: string[];
  stop_authority: string;
  pending_order_treatment: string;
  remaining_position_policy: string;
  approving_actors?: string[];
};

export async function createStockLearningCycleApproval(
  cycleId: string,
  body: CreatePaperRunApprovalRequest,
): Promise<StockLearningCycle> {
  return postJson<StockLearningCycle>(
    `/stock/learning-cycles/${encodeURIComponent(cycleId)}/approval`,
    body,
  );
}

export type StockLearningCycleAction = "mark_eligible" | "start_canary" | "promote" | "demote" | "retire";

export async function actOnStockLearningCycle(
  cycleId: string,
  action: StockLearningCycleAction,
  reason: string,
): Promise<StockLearningCycle> {
  return postJson<StockLearningCycle>(`/stock/learning-cycles/${encodeURIComponent(cycleId)}/action`, { action, reason });
}

export async function getStockLearningCycle(cycleId: string): Promise<StockLearningCycle> {
  const response = await authenticatedFetch(`${API_BASE_URL}/stock/learning-cycles/${encodeURIComponent(cycleId)}`, { cache: "no-store" });
  return handleResponse<StockLearningCycle>(response);
}

export type LaunchPrerequisites = {
  cycle_id: string | null;
  checked_at: string;
  status: "blocked" | "unknown_outside_session" | "unknown" | "ready";
  eligible_for_approval: boolean;
  reason: string;
  gates: Record<string, { status: string; reason?: string; evidence?: unknown }>;
};

export async function getLaunchPrerequisites(cycleId?: string): Promise<LaunchPrerequisites> {
  const query = cycleId ? `?cycle_id=${encodeURIComponent(cycleId)}` : "";
  const response = await authenticatedFetch(`${API_BASE_URL}/stock/learning-cycles/launch-prerequisites${query}`, { cache: "no-store" });
  return handleResponse<LaunchPrerequisites>(response);
}

export type StockLearningCycle = {
  cycle_id: string;
  status: string;
  stage: string;
  trigger: string;
  requested_by: string;
  symbols: string[];
  cutoff_date: string;
  horizon_days: number;
  provider: string;
  snapshot_id: string | null;
  training_job_id: string | null;
  model_run_id: string | null;
  binding_id: number | null;
  trial_id: string | null;
  active_binding_id: number | null;
  active_binding_model_run_id: string | null;
  handoff: {
    stage: string;
    status: string;
    binding_id: number | null;
    trial_id: string | null;
    trial_status: string | null;
    preflight: { status: string; reason?: string; evidence?: unknown } | null;
    approval: PaperRunApproval;
    reason: string | null;
    report_id: number | null;
    report_decision: string | null;
  };
  position_handling: {
    approved_policy: "hold" | "reduce" | "flatten" | string | null;
    stop_status: "not_started" | "running" | "paused" | "expired" | "operator_stopped" | string;
    stop_reason: string | null;
    stopped_at: string | null;
    new_entries_stopped: boolean;
    handling_status: "not_started" | "not_stopped" | "held" | "reduction_pending" | "reduced" | "flatten_pending" | "flattened" | "unknown" | string;
    remaining_positions: Array<{
      symbol: string;
      quantity: string;
      market_value: string | null;
      observed_at: string | null;
    }>;
    managed_lots: Array<{
      symbol: string;
      entry_quantity: string;
      exited_quantity: string;
      remaining_quantity: string;
      exit_reason: string | null;
      exit_status: string | null;
      exit_decided_at: string | null;
    }>;
    has_exit_intent: boolean;
    reconciliation: {
      status: string;
      reconciliation_required: boolean;
      last_reconciled_at: string | null;
    };
  };
  gates: Record<string, { status: string; reason?: string; evidence?: unknown }>;
  evidence: Record<string, unknown>;
  last_reason: string | null;
  scheduled_learning_paused: boolean;
  paper_only: boolean;
  live_authorized: boolean;
  automatic_promotion: StockPaperPromotionDecision | null;
  monitoring: {
    snapshot_id: number | null;
    status: string;
    generated_at: string | null;
    actions: Array<Record<string, unknown>>;
  };
  recovery: {
    status: string;
    last_known_good_model_run_id: string | null;
    last_known_good_binding_id: number | null;
    latest_event_id: number | null;
    latest_event_action: string | null;
  };
  events: StockLearningCycleEvent[];
  created_at: string;
  updated_at: string;
  deduplicated?: boolean;
};

export type StockPaperPromotionDecision = {
  id: number;
  cycle_id: string;
  trial_id: string | null;
  report_id: number | null;
  model_run_id: string | null;
  snapshot_id: string | null;
  decision: string;
  gates: Record<string, { status: string; reason?: string; evidence?: unknown }>;
  lineage: Record<string, unknown>;
  evidence: Record<string, unknown>;
  actor: string;
  source_job: string;
  correlation_id: string;
  reason: string;
  before_binding_id: number | null;
  before_model_run_id: string | null;
  after_binding_id: number | null;
  after_model_run_id: string | null;
  decision_sha256: string;
  paper_only: boolean;
  live_authorized: boolean;
  created_at: string;
};
