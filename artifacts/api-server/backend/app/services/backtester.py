from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from app.services.backtest_statistics import (
    block_bootstrap_ci,
    deflated_sharpe_ratio,
    equity_returns,
    return_moments,
)
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
    # "volatility_scaled" (default) adds a volatility-proportional spread /
    # slippage component and square-root market impact on top of the flat
    # ``slippage_bps`` floor, and caps entries at ``max_participation`` of
    # prior average daily volume. "flat" reproduces the legacy fixed-bps model.
    cost_model: str = "volatility_scaled"
    volatility_lookback: int = 20
    volatility_cost_fraction: float = 0.05
    impact_coefficient: float = 1.0
    max_participation: float = 0.01
    bootstrap_samples: int = 1000
    bootstrap_seed: int = 7


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
    # Passive-baseline and statistical-confidence gates. They only add
    # rejection reasons (tightening); legacy metric dicts without these
    # sections are evaluated exactly as before.
    excess = metrics.get("excess_return") or {}
    if excess.get("vs_cash") is not None and excess["vs_cash"] <= 0:
        reasons.append("Strategy does not beat holding cash after costs.")
    if excess.get("vs_exposure_matched_buy_and_hold") is not None and excess["vs_exposure_matched_buy_and_hold"] <= 0:
        reasons.append("Strategy does not beat an exposure-matched buy-and-hold of the same symbol after costs.")
    bootstrap = (metrics.get("statistics") or {}).get("bootstrap")
    if bootstrap is not None:
        if bootstrap.get("status") != "ok":
            reasons.append("Too few return observations for a bootstrap confidence interval.")
        elif bootstrap["mean_return_ci"][0] <= 0:
            reasons.append("95% bootstrap confidence interval of mean daily return does not exclude zero.")
    return reasons


def downside_deviation(returns: pd.Series) -> float:
    """Return per-period downside RMS deviation around a zero target.

    Sortino's denominator is conventionally the root mean square of negative
    returns over *all* periods, not the sample standard deviation of the
    subset of negative observations.  Including zeroes for non-loss periods
    also keeps sparse-loss series comparable and avoids a two-observation
    special case.
    """
    negative = returns.clip(upper=0.0)
    return float((negative.pow(2).mean() ** 0.5)) if len(returns) else 0.0


def _ohlc(bar: pd.Series) -> tuple[float, float, float, float, bool]:
    """Return open/high/low/close; fall back to the open-close envelope.

    Without a recorded high/low the price still traversed every value between
    the open and close, so the envelope is a conservative minimum range.
    """
    open_, close = float(bar["open"]), float(bar["close"])
    has_range = all(name in bar.index and pd.notna(bar[name]) for name in ("high", "low"))
    high = float(bar["high"]) if has_range else max(open_, close)
    low = float(bar["low"]) if has_range else min(open_, close)
    return open_, max(high, open_, close), min(low, open_, close), close, has_range


def _liquidity(history: pd.DataFrame, config: BacktestConfig) -> tuple[float, Optional[float]]:
    """Causal daily volatility (bps) and average volume from prior bars only."""
    window = history.tail(config.volatility_lookback + 1)
    sigma_bps = 0.0
    if len(window) > 2:
        closes = pd.to_numeric(window["close"], errors="coerce").astype(float)
        log_returns = np.log(closes).diff().dropna()
        if len(log_returns) > 1:
            sigma = float(log_returns.std(ddof=1))
            sigma_bps = sigma * 10_000 if math.isfinite(sigma) else 0.0
    adv = None
    if "volume" in history.columns and len(history):
        volume = pd.to_numeric(history["volume"].tail(config.volatility_lookback), errors="coerce").astype(float)
        average = float(volume.mean()) if len(volume) else 0.0
        adv = average if math.isfinite(average) and average > 0 else None
    return sigma_bps, adv


def execution_cost_bps(history: pd.DataFrame, quantity: float, config: BacktestConfig) -> dict:
    """Adverse price move per side, in bps, estimated from prior bars only."""
    base = float(config.slippage_bps)
    if config.cost_model == "flat":
        return {"total_bps": base, "base_bps": base, "volatility_bps": 0.0, "impact_bps": 0.0, "participation": None}
    if config.cost_model != "volatility_scaled":
        raise ValueError(f"Unknown cost model: {config.cost_model}")
    sigma_bps, adv = _liquidity(history, config)
    volatility_bps = config.volatility_cost_fraction * sigma_bps
    participation = quantity / adv if adv and quantity > 0 else None
    impact_bps = config.impact_coefficient * sigma_bps * math.sqrt(participation) if participation else 0.0
    # Keep execution proceeds positive even for pathological inputs.
    total = min(base + volatility_bps + impact_bps, 5_000.0)
    return {"total_bps": total, "base_bps": base, "volatility_bps": volatility_bps,
            "impact_bps": impact_bps, "participation": participation}


def _curve_metrics(values: list[float]) -> dict:
    series = pd.Series(values, dtype=float)
    returns = series.pct_change().dropna()
    total_return = float(series.iloc[-1] / series.iloc[0] - 1) if len(series) else 0.0
    sharpe = (returns.mean() / returns.std() * math.sqrt(252)) if len(returns) > 1 and returns.std() else 0
    drawdown = (series / series.cummax() - 1).min() if len(series) else 0
    periods = max(len(series) - 1, 1)
    return {
        "total_return": round(total_return, 4),
        "annualized_return": round((1 + total_return) ** (252 / periods) - 1, 4),
        "sharpe_ratio": round(float(sharpe), 4),
        "max_drawdown": round(abs(float(drawdown)), 4),
    }


def _buy_and_hold_values(frame: pd.DataFrame, start_idx: int, config: BacktestConfig) -> Optional[list[float]]:
    """Capital-anchored value path of a full-capital buy at ``start_idx`` open.

    Uses the same fee and execution-cost model as the strategy (including
    impact), liquidates at the final close, but is not volume-capped.
    """
    if start_idx >= len(frame):
        return None
    cash = float(config.starting_cash)
    bar = frame.iloc[start_idx]
    open_ = float(bar["open"])
    history = frame.iloc[:start_idx]
    estimate = cash / open_
    entry_price = open_ * (1 + execution_cost_bps(history, estimate, config)["total_bps"] / 10_000)
    quantity = cash / (entry_price * (1 + config.fees_bps / 10_000))
    residual = cash - quantity * entry_price * (1 + config.fees_bps / 10_000)
    values = [cash] + [residual + quantity * float(close) for close in frame["close"].iloc[start_idx:]]
    exit_price = float(frame["close"].iloc[-1]) * (1 - execution_cost_bps(frame, quantity, config)["total_bps"] / 10_000)
    values[-1] = residual + quantity * exit_price * (1 - config.fees_bps / 10_000)
    return values


def _benchmark_window(benchmark: pd.DataFrame, first_date, last_date) -> tuple[Optional[pd.DataFrame], int]:
    dates = pd.to_datetime(benchmark["date"], errors="coerce", utc=True)
    first, last = pd.to_datetime(first_date, utc=True), pd.to_datetime(last_date, utc=True)
    keep = (dates <= last).to_numpy()
    frame = benchmark.loc[keep].reset_index(drop=True)
    positions = np.flatnonzero((dates[keep] >= first).to_numpy())
    if len(positions) < 2:
        return None, 0
    return frame, int(positions[0])


def _baselines(symbol: str, prices: pd.DataFrame, start_idx: int, strategy_values: list[float],
               average_exposure: float, config: BacktestConfig,
               benchmark_prices: Optional[pd.DataFrame], benchmark_symbol: str) -> tuple[dict, dict, dict]:
    convention = ("Same starting cash, evaluation window, fees and execution-cost model as the strategy; "
                  "buy at the first evaluated open, liquidate at the final close; no participation cap.")
    baselines: dict = {"convention": convention,
                       "cash": {"total_return": 0.0, "annualized_return": 0.0, "sharpe_ratio": 0.0, "max_drawdown": 0.0}}
    strategy_return = strategy_values[-1] / strategy_values[0] - 1
    excess: dict = {"vs_cash": round(strategy_return, 4)}
    bh_values = _buy_and_hold_values(prices, start_idx, config) if start_idx < len(prices) else None
    if bh_values:
        baselines["buy_and_hold"] = {"symbol": symbol, **_curve_metrics(bh_values)}
        exposure = min(max(average_exposure, 0.0), 1.0)
        matched = [(1 - exposure) * bh_values[0] + exposure * value for value in bh_values]
        baselines["exposure_matched_buy_and_hold"] = {"symbol": symbol, "exposure": round(exposure, 4),
                                                      **_curve_metrics(matched)}
        excess["vs_buy_and_hold"] = round(strategy_return - (bh_values[-1] / bh_values[0] - 1), 4)
        excess["vs_exposure_matched_buy_and_hold"] = round(strategy_return - (matched[-1] / matched[0] - 1), 4)
    else:
        baselines["buy_and_hold"] = {"status": "unavailable", "reason": "No evaluated bars."}
        excess["vs_buy_and_hold"] = excess["vs_exposure_matched_buy_and_hold"] = None

    spy: dict = {"status": "unavailable", "reason": f"{benchmark_symbol} prices were not supplied."}
    excess[f"vs_{benchmark_symbol.lower()}_buy_and_hold"] = None
    if symbol.upper() == benchmark_symbol.upper() and bh_values:
        spy = dict(baselines["buy_and_hold"])
    elif benchmark_prices is not None and len(benchmark_prices) and bh_values:
        window, bench_start = _benchmark_window(benchmark_prices, prices.iloc[start_idx]["date"], prices.iloc[-1]["date"])
        values = _buy_and_hold_values(window, bench_start, config) if window is not None else None
        if values:
            spy = {"symbol": benchmark_symbol, **_curve_metrics(values)}
        else:
            spy = {"status": "unavailable", "reason": f"{benchmark_symbol} history does not cover the evaluation window."}
    if "total_return" in spy:
        excess[f"vs_{benchmark_symbol.lower()}_buy_and_hold"] = round(strategy_return - spy["total_return"], 4)
    baselines[f"{benchmark_symbol.lower()}_buy_and_hold"] = spy
    beats = {name.removeprefix("vs_"): (None if value is None else bool(value > 0)) for name, value in excess.items()}
    return baselines, excess, beats


def run_backtest(
    symbol: str,
    strategy_slug: str,
    prices: pd.DataFrame,
    config: Optional[BacktestConfig] = None,
    parameters: Optional[dict] = None,
    *,
    benchmark_prices: Optional[pd.DataFrame] = None,
    benchmark_symbol: str = "SPY",
) -> dict:
    config = config or BacktestConfig()
    strategy = get_strategy(strategy_slug, parameters)
    cash = config.starting_cash
    position = None
    trades: list[dict] = []
    equity_curve: list[dict] = []
    exposures: list[float] = []
    participation_capped_entries = 0
    range_from_high_low = True

    def close_position(raw_price: float, reason: str, date: str, history: pd.DataFrame) -> float:
        nonlocal position
        cost = execution_cost_bps(history, position["quantity"], config)
        exit_price = raw_price * (1 - cost["total_bps"] / 10_000)
        proceeds = position["quantity"] * exit_price
        fee = proceeds * config.fees_bps / 10_000
        pnl = proceeds - fee - position["cost"]
        trades.append(
            {
                "entry_date": position["entry_date"],
                "exit_date": date,
                "entry_price": round(position["entry_price"], 2),
                "exit_price": round(exit_price, 2),
                "quantity": round(position["quantity"], 4),
                "profit_loss": round(pnl, 2),
                "profit_loss_pct": round(pnl / position["cost"], 4),
                "exit_reason": reason,
                "exit_cost_bps": round(cost["total_bps"], 2),
            }
        )
        position = None
        return proceeds - fee

    min_history = 60
    # A decision can only use the last completed bar. Execute it at the next
    # bar's open so the backtest cannot use the fill bar's close as input.
    for idx in range(min_history + 1, len(prices)):
        history = prices.iloc[:idx]
        execution_bar = prices.iloc[idx]
        current_date = str(execution_bar["date"])
        open_, high, low, mark_price, has_range = _ohlc(execution_bar)
        range_from_high_low = range_from_high_low and has_range

        # 1. Checks at the open: a gap through a level fills at the open,
        #    never at the (better) level price.
        if position:
            holding_days = idx - position["entry_idx"]
            if open_ <= position["stop_level"]:
                cash += close_position(open_, "stop_loss_gap", current_date, history)
            elif open_ >= position["target_level"]:
                cash += close_position(open_, "take_profit_gap", current_date, history)
            elif holding_days >= config.max_holding_days:
                cash += close_position(open_, "max_holding_period", current_date, history)

        # 2. New entry at the open, using only completed prior bars.
        if position is None:
            signal = strategy.generate_signal(symbol, history)
            if signal.action == "BUY" and signal.confidence >= 0.55:
                # Size from the floor-cost price; impact is charged after the cap.
                reference_price = open_ * (1 + config.slippage_bps / 10_000)
                max_risk_dollars = cash * config.risk_per_trade
                quantity = max_risk_dollars / max(reference_price * config.stop_loss_pct, 1)
                if config.cost_model != "flat":
                    _sigma, adv = _liquidity(history, config)
                    if adv is not None and quantity > config.max_participation * adv:
                        quantity = config.max_participation * adv
                        participation_capped_entries += 1
                entry_cost = execution_cost_bps(history, quantity, config)
                entry_price = open_ * (1 + entry_cost["total_bps"] / 10_000)
                cost = quantity * entry_price
                fee = cost * config.fees_bps / 10_000
                if quantity > 0 and cost + fee <= cash:
                    cash -= cost + fee
                    position = {
                        "entry_idx": idx,
                        "entry_date": current_date,
                        "entry_price": entry_price,
                        "quantity": quantity,
                        "cost": cost + fee,
                        "stop_level": entry_price * (1 - config.stop_loss_pct),
                        "target_level": entry_price * (1 + config.take_profit_pct),
                    }

        # 3. Intrabar checks after the open (including the entry bar). When
        #    the bar touches both levels the order is unknown; assume the
        #    stop was hit first (conservative).
        if position:
            if low <= position["stop_level"]:
                cash += close_position(position["stop_level"], "stop_loss_intrabar", current_date, history)
            elif high >= position["target_level"]:
                cash += close_position(position["target_level"], "take_profit_intrabar", current_date, history)

        position_value = position["quantity"] * mark_price if position else 0.0
        marked_value = cash + position_value
        exposures.append(position_value / marked_value if marked_value > 0 else 0.0)
        equity_curve.append({"date": current_date, "value": round(marked_value, 2)})

    # Realize any position still open at the end of the available history,
    # charging the same exit costs to both the ledger and the final equity
    # point so total return is net of liquidation.
    if position:
        final_bar = prices.iloc[-1]
        cash += close_position(float(final_bar["close"]), "end_of_history", str(final_bar["date"]), prices)
        equity_curve[-1]["value"] = round(cash, 2)

    # Anchor risk statistics to capital before the first fill. Otherwise the
    # first evaluated bar's P&L disappears from returns and its loss becomes
    # the initial high-water mark, understating drawdown.
    values = pd.Series([config.starting_cash] + [row["value"] for row in equity_curve], dtype=float)
    returns = values.pct_change().dropna()
    ending_value = equity_curve[-1]["value"] if equity_curve else config.starting_cash
    total_return = ending_value / config.starting_cash - 1
    annualized_return = (1 + total_return) ** (252 / max(len(equity_curve), 1)) - 1
    sharpe = (returns.mean() / returns.std() * math.sqrt(252)) if len(returns) > 1 and returns.std() else 0
    downside = downside_deviation(returns)
    sortino = (returns.mean() / downside * math.sqrt(252)) if downside else 0
    drawdown = (values / values.cummax() - 1).min() if len(values) else 0
    wins = [trade for trade in trades if trade["profit_loss"] > 0]
    losses = [trade for trade in trades if trade["profit_loss"] <= 0]
    gross_profit = sum(trade["profit_loss"] for trade in wins)
    gross_loss = abs(sum(trade["profit_loss"] for trade in losses))
    profit_factor = gross_profit / gross_loss if gross_loss else (3.0 if gross_profit else 0.0)
    win_rate = len(wins) / len(trades) if trades else 0
    average_exposure = float(np.mean(exposures)) if exposures else 0.0

    start_idx = min_history + 1
    baselines, excess, beats = _baselines(
        symbol, prices, start_idx, values.tolist(), average_exposure, config, benchmark_prices, benchmark_symbol,
    )
    period_returns = equity_returns(values.tolist())
    moments = return_moments(period_returns)
    statistics = {
        "observations": moments["observations"],
        "mean_period_return": round(moments["mean"], 8),
        "std_period_return": round(moments["std"], 8),
        "skewness": round(moments["skewness"], 4),
        "kurtosis": round(moments["kurtosis"], 4),
        "sharpe_per_period": round(moments["sharpe_per_period"], 6),
        # A single run is one trial. Callers that compare several parameter
        # sets on the same history must deflate with their own trial count.
        "probabilistic_sharpe": deflated_sharpe_ratio(
            moments["sharpe_per_period"], moments["observations"], trials=1,
            skewness=moments["skewness"], kurtosis=moments["kurtosis"],
        ),
        "bootstrap": block_bootstrap_ci(period_returns, samples=config.bootstrap_samples, seed=config.bootstrap_seed),
    }
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
        "profit_factor_capped": bool(not gross_loss and gross_profit),
        "number_of_trades": len(trades),
        "equity_curve": equity_curve,
        "trades": trades,
        "execution_model": "next_open_no_lookahead",
        "exit_model": "intrabar_high_low_stop_first" if range_from_high_low else "intrabar_open_close_envelope_stop_first",
        "cost_model": {
            "name": config.cost_model,
            "fees_bps": config.fees_bps,
            "base_slippage_bps": config.slippage_bps,
            "volatility_cost_fraction": config.volatility_cost_fraction if config.cost_model != "flat" else 0.0,
            "impact_coefficient": config.impact_coefficient if config.cost_model != "flat" else 0.0,
            "max_participation": config.max_participation if config.cost_model != "flat" else None,
            "participation_capped_entries": participation_capped_entries,
        },
        "average_exposure": round(average_exposure, 4),
        "baselines": baselines,
        "excess_return": excess,
        "beats_baselines": beats,
        "statistics": statistics,
    }
    metrics["score"] = normalized_score(metrics)
    reasons = rejection_reasons(metrics)
    metrics["rejected"] = bool(reasons)
    metrics["rejection_reasons"] = reasons
    return metrics
