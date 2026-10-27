from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class PortfolioState:
    daily_drawdown: float = 0.0
    open_positions_count: int = 0
    open_positions_by_symbol: Optional[dict[str, float]] = None
    open_positions_by_strategy: Optional[dict[str, int]] = None
    total_equity: float = 100_000
    kill_switch_enabled: bool = False


@dataclass
class StrategyState:
    status: str = "paper_trading_active"
    drawdown: float = 0.0
    consecutive_losses: int = 0


DEFAULT_RISK_RULES = {
    "min_confidence": 0.58,
    "max_daily_drawdown": 0.03,
    "max_strategy_drawdown": 0.10,
    "max_open_positions": 10,
    "max_open_positions_per_strategy": 5,
    "max_symbol_exposure": 0.10,
    # Live execution uses these only when explicitly enabled.  The defaults
    # preserve the paper allocation shape and do not introduce larger limits.
    "max_total_exposure": 1.0,
    "max_live_open_orders": 10,
    "max_live_slippage": 0.02,
    "max_risk_per_trade": 0.01,
    "max_live_turnover": 0.50,
    "max_order_liquidity_fraction": 0.10,
    "max_live_leverage": 1.0,
    "stop_after_consecutive_losses": 5,
    "candidate_review_score_threshold": 0.70,
    "activation_score_threshold": 0.72,
    "journal_feedback_review_threshold_cap": 0.03,
    "journal_feedback_allocation_multiplier_cap": 0.20,
    "memory_replay_min_complete_samples": 10,
    "memory_replay_min_avg_return_delta": 0.0,
    "memory_replay_min_hit_rate_delta": 0.0,
    "paper_only": True,
}


def _finite(value) -> Optional[float]:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def approve_trade(signal: dict, portfolio: PortfolioState, strategy_state: StrategyState, risk_rules: Optional[dict] = None) -> tuple[bool, str]:
    """Deterministic pre-trade gate.  Every check fails closed.

    Optional ``signal["proposed_exposure"]`` (fraction of equity the order
    would add) is included in the symbol and gross caps when supplied, so a
    symbol just under its cap cannot be pushed far over it by one order.
    """
    rules = DEFAULT_RISK_RULES | (risk_rules or {})
    symbol = signal.get("symbol", "")
    strategy = str(signal.get("strategy", ""))
    exposures = portfolio.open_positions_by_symbol or {}
    strategy_positions = portfolio.open_positions_by_strategy or {}

    if portfolio.kill_switch_enabled:
        return False, "Global kill switch is enabled."
    if not rules.get("paper_only", True):
        return False, "Live trading is disabled in Version 1."
    if strategy_state.status != "paper_trading_active":
        return False, "Strategy is not active for paper trading."
    if signal.get("action") not in {"BUY", "SELL"}:
        return False, "Signal is not actionable."
    # NaN compares False against every threshold, so a NaN confidence or
    # drawdown used to pass these gates.  Non-finite inputs are rejected.
    confidence = _finite(signal.get("confidence", 0))
    if confidence is None:
        return False, "Signal confidence is missing or not finite."
    if confidence < float(rules["min_confidence"]):
        return False, "Confidence below minimum threshold."
    daily_drawdown = _finite(portfolio.daily_drawdown)
    if daily_drawdown is None:
        return False, "Daily drawdown is unknown."
    # A drawdown exactly at the limit is a breach (portfolio_risk alerts at >=).
    if daily_drawdown >= float(rules["max_daily_drawdown"]):
        return False, "Daily drawdown limit exceeded."
    strategy_drawdown = _finite(strategy_state.drawdown)
    if strategy_drawdown is None:
        return False, "Strategy drawdown is unknown."
    if strategy_drawdown >= float(rules["max_strategy_drawdown"]):
        return False, "Strategy drawdown limit exceeded."
    if strategy_state.consecutive_losses >= int(rules["stop_after_consecutive_losses"]):
        return False, "Strategy hit consecutive-loss pause rule."
    if portfolio.open_positions_count >= int(rules["max_open_positions"]):
        return False, "Portfolio has too many open positions."
    if strategy and strategy_positions.get(strategy, 0) >= int(rules["max_open_positions_per_strategy"]):
        return False, "Strategy has too many open positions."
    exposure_values = [_finite(value) for value in exposures.values()]
    if any(value is None for value in exposure_values):
        return False, "Open exposure is not finite."
    proposed = 0.0
    if signal.get("proposed_exposure") is not None:
        proposed = _finite(signal.get("proposed_exposure"))
        if proposed is None or proposed < 0:
            return False, "Proposed exposure is not a finite non-negative fraction."
    symbol_limit = float(rules["max_symbol_exposure"])
    symbol_exposure = float(exposures.get(symbol, 0) or 0)
    if symbol_exposure >= symbol_limit:
        return False, "Symbol exposure limit reached."
    if proposed and symbol_exposure + proposed > symbol_limit + 1e-9:
        return False, "Proposed order would exceed the symbol exposure limit."
    # Per-symbol caps alone allow N symbols x cap of gross exposure; the
    # aggregate cap bounds the whole book.
    gross_limit = float(rules.get("max_total_exposure", 1.0))
    gross_exposure = sum(abs(value) for value in exposure_values if value is not None)
    if gross_exposure >= gross_limit:
        return False, "Gross exposure limit reached."
    if proposed and gross_exposure + proposed > gross_limit + 1e-9:
        return False, "Proposed order would exceed the gross exposure limit."

    return True, "Approved for paper trading."


ATR_STOP_MULTIPLE = 2.0


def volatility_capped_exposure(
    atr_pct: Optional[float],
    rules: Optional[dict] = None,
    *,
    stop_multiple: float = ATR_STOP_MULTIPLE,
) -> Optional[float]:
    """Largest equity fraction whose ``stop_multiple`` x ATR stop loses at most
    ``max_risk_per_trade`` of equity.

    Returns ``None`` when ATR is unavailable so callers keep their existing
    cap rather than silently sizing to zero or to infinity.
    """
    merged = DEFAULT_RISK_RULES | (rules or {})
    atr = _finite(atr_pct)
    if atr is None or atr <= 0:
        return None
    risk_budget = max(float(merged.get("max_risk_per_trade", 0.01)), 0.0)
    return risk_budget / (max(float(stop_multiple), 0.1) * atr)
