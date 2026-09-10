from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from app.services.strategies.registry import get_strategy


@dataclass
class BacktestConfig:
    starting_cash: float = 100_000
    risk_per_trade: float = 0.01
    fees_bps: float = 1
    slippage_bps: float = 5
    stop_loss_pct: float = 0.03
    take_profit_pct: float = 0.06
    max_holding_days: int = 20


def normalized_score(metrics: dict) -> float:
    total_return = min(max(metrics["total_return"], -0.5), 0.5) / 0.5
    sharpe = min(max(metrics["sharpe_ratio"], -1.0), 3.0) / 3.0
    profit_factor = min(max(metrics["profit_factor"], 0), 3.0) / 3.0
    win_rate = metrics["win_rate"]
    drawdown = min(max(metrics["max_drawdown"], 0), 0.5) / 0.5
    return round(0.30 * total_return + 0.25 * sharpe + 0.20 * profit_factor + 0.15 * win_rate - 0.30 * drawdown, 4)


def rejection_reasons(metrics: dict) -> list[str]:
    reasons = []
    if metrics["max_drawdown"] > 0.20:
        reasons.append("Max drawdown exceeds 20%.")
    if metrics["number_of_trades"] < 30:
        reasons.append("Fewer than 30 historical trades.")
    if metrics["profit_factor"] < 1.1:
        reasons.append("Profit factor below 1.1.")
    if metrics["win_rate"] < 0.45:
        reasons.append("Win rate below 45%.")
    if metrics["score"] < 0.15:
        reasons.append("Strategy score below minimum threshold.")
    return reasons


def run_backtest(symbol: str, strategy_slug: str, prices: pd.DataFrame, config: Optional[BacktestConfig] = None, parameters: Optional[dict] = None) -> dict:
    config = config or BacktestConfig()
    strategy = get_strategy(strategy_slug, parameters)
    cash = config.starting_cash
    position = None
    trades: list[dict] = []
    equity_curve: list[dict] = []

    min_history = 60
    for idx in range(min_history, len(prices)):
        history = prices.iloc[: idx + 1]
        current = history.iloc[-1]
        current_date = str(current["date"])
        price = float(current["close"])

        if position:
            holding_days = idx - position["entry_idx"]
            pnl_pct = (price - position["entry_price"]) / position["entry_price"]
            should_exit = (
                pnl_pct <= -config.stop_loss_pct
                or pnl_pct >= config.take_profit_pct
                or holding_days >= config.max_holding_days
            )
            if should_exit:
                exit_price = price * (1 - config.slippage_bps / 10_000)
                proceeds = position["quantity"] * exit_price
                fee = proceeds * config.fees_bps / 10_000
                pnl = proceeds - fee - position["cost"]
                cash += proceeds - fee
                trades.append(
                    {
                        "entry_date": position["entry_date"],
                        "exit_date": current_date,
                        "entry_price": round(position["entry_price"], 2),
                        "exit_price": round(exit_price, 2),
                        "quantity": round(position["quantity"], 4),
                        "profit_loss": round(pnl, 2),
                        "profit_loss_pct": round(pnl / position["cost"], 4),
                    }
                )
                position = None

        if position is None:
            signal = strategy.generate_signal(symbol, history)
            if signal.action == "BUY" and signal.confidence >= 0.55:
                entry_price = price * (1 + config.slippage_bps / 10_000)
                max_risk_dollars = cash * config.risk_per_trade
                quantity = max_risk_dollars / max(entry_price * config.stop_loss_pct, 1)
                cost = quantity * entry_price
                if cost <= cash:
                    fee = cost * config.fees_bps / 10_000
                    cash -= cost + fee
                    position = {
                        "entry_idx": idx,
                        "entry_date": current_date,
                        "entry_price": entry_price,
                        "quantity": quantity,
                        "cost": cost + fee,
                    }

        marked_value = cash + (position["quantity"] * price if position else 0)
        equity_curve.append({"date": current_date, "value": round(marked_value, 2)})

    returns = pd.Series([row["value"] for row in equity_curve]).pct_change().dropna()
    ending_value = equity_curve[-1]["value"] if equity_curve else config.starting_cash
    total_return = ending_value / config.starting_cash - 1
    annualized_return = (1 + total_return) ** (252 / max(len(equity_curve), 1)) - 1
    sharpe = (returns.mean() / returns.std() * math.sqrt(252)) if len(returns) > 1 and returns.std() else 0
    downside = returns[returns < 0]
    sortino = (returns.mean() / downside.std() * math.sqrt(252)) if len(downside) > 1 and downside.std() else 0
    values = pd.Series([row["value"] for row in equity_curve])
    drawdown = (values / values.cummax() - 1).min() if len(values) else 0
    wins = [trade for trade in trades if trade["profit_loss"] > 0]
    losses = [trade for trade in trades if trade["profit_loss"] <= 0]
    gross_profit = sum(trade["profit_loss"] for trade in wins)
    gross_loss = abs(sum(trade["profit_loss"] for trade in losses))
    profit_factor = gross_profit / gross_loss if gross_loss else (3.0 if gross_profit else 0.0)
    win_rate = len(wins) / len(trades) if trades else 0
    metrics = {
        "symbol": symbol,
        "strategy": strategy_slug,
        "total_return": round(total_return, 4),
        "annualized_return": round(annualized_return, 4),
        "sharpe_ratio": round(float(sharpe), 4),
        "sortino_ratio": round(float(sortino), 4),
        "max_drawdown": round(abs(float(drawdown)), 4),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(float(profit_factor), 4),
        "number_of_trades": len(trades),
        "equity_curve": equity_curve,
        "trades": trades,
    }
    metrics["score"] = normalized_score(metrics)
    reasons = rejection_reasons(metrics)
    metrics["rejected"] = bool(reasons)
    metrics["rejection_reasons"] = reasons
    return metrics
