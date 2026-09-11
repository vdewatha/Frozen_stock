from __future__ import annotations

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
    "max_risk_per_trade": 0.01,
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


def approve_trade(signal: dict, portfolio: PortfolioState, strategy_state: StrategyState, risk_rules: Optional[dict] = None) -> tuple[bool, str]:
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
    if float(signal.get("confidence", 0)) < float(rules["min_confidence"]):
        return False, "Confidence below minimum threshold."
    if portfolio.daily_drawdown > float(rules["max_daily_drawdown"]):
        return False, "Daily drawdown limit exceeded."
    if strategy_state.drawdown > float(rules["max_strategy_drawdown"]):
        return False, "Strategy drawdown limit exceeded."
    if strategy_state.consecutive_losses >= int(rules["stop_after_consecutive_losses"]):
        return False, "Strategy hit consecutive-loss pause rule."
    if portfolio.open_positions_count >= int(rules["max_open_positions"]):
        return False, "Portfolio has too many open positions."
    if strategy and strategy_positions.get(strategy, 0) >= int(rules["max_open_positions_per_strategy"]):
        return False, "Strategy has too many open positions."
    if exposures.get(symbol, 0) >= float(rules["max_symbol_exposure"]):
        return False, "Symbol exposure limit reached."

    return True, "Approved for paper trading."
