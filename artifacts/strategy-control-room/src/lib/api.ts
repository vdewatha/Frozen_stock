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
  equity_curve: { date: string; value: number }[];
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

const configuredApi = import.meta.env.VITE_API_BASE_URL ?? "/api";
const API_BASE_URL = configuredApi.replace(/\/$/, "");

let accessToken = "";
export function setAccessToken(value: string) { accessToken = value; }
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
  if (!accessToken) throw new Error("Sign in to access the trading service.");
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${accessToken}`);
  const response = await globalThis.fetch(input, { ...init, headers, cache: "no-store", credentials: "omit" });
  if (!response.ok) throw new ApiError(response.status, await responseDetail(response));
  return response;
}

export async function getDashboard(): Promise<DashboardSnapshot> {
  const response = await authenticatedFetch(`${API_BASE_URL}/dashboard`);
  return handleResponse<DashboardSnapshot>(response);
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
