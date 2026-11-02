"""Isolated short-only historical paper simulation; never calls a broker.

Not registered in the long-only strategy registry: SELL must not accidentally
be interpreted as a long exit or promoted into broker execution.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from app.services.strategies.short_term_reversion import wilder_rsi


class ShortSimulationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    starting_equity: float = Field(default=100_000.0, gt=0)
    allocation: float = Field(default=0.05, gt=0, le=0.10)
    trend_window: int = Field(default=200, ge=3, le=500)
    entry_rsi: float = Field(default=90.0, gt=50, lt=100)
    exit_rsi: float = Field(default=50.0, gt=0, le=50)
    stop_loss_pct: float = Field(default=0.03, gt=0, le=0.10)
    max_holding_sessions: int = Field(default=5, ge=1, le=20)
    fee_bps: float = Field(default=1.0, ge=0, le=100)
    slippage_bps: float = Field(default=5.0, ge=0, le=100)
    annual_borrow_rate: float = Field(default=0.03, ge=0, le=1)


def validated_short_history(frame: pd.DataFrame, *, source: str,
                            config: ShortSimulationConfig | None = None,
                            completed_before=None):
    """Shared dated OHLC validation for research and the paper-only scheduler."""
    config = config or ShortSimulationConfig()
    if source not in {"yfinance", "yahoo_chart", "alpaca_iex_daily"}:
        raise ValueError("Short simulation requires one supported real historical data source")
    required = {"date", "open", "high", "low", "close"}
    if frame.columns.duplicated().any():
        raise ValueError("Daily history must not contain duplicate columns")
    if "source" in frame.columns and not frame["source"].eq(source).all():
        raise ValueError("Daily history must use one consistent source")
    if not required.issubset(frame.columns) or len(frame) < config.trend_window + 2:
        raise ValueError("Insufficient dated OHLC history for short simulation")
    data = frame[list(sorted(required))].copy().reset_index(drop=True)
    dates = pd.to_datetime(data["date"], errors="raise", utc=True)
    if dates.isna().any() or dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise ValueError("Daily dates must be unique and strictly increasing")
    if (dates != dates.dt.normalize()).any():
        raise ValueError("Expected daily session dates, not intraday bars")
    if (dates.dt.date >= (completed_before or datetime.now(timezone.utc).date())).any():
        raise ValueError("Only completed historical sessions are supported")
    prices = data[["open", "high", "low", "close"]]
    if prices.map(lambda value: isinstance(value, (bool, np.bool_))).any().any():
        raise ValueError("Boolean values are not market prices")
    values = prices.apply(pd.to_numeric, errors="raise")
    if not np.isfinite(values.to_numpy(dtype=float)).all() or (values <= 0).any().any():
        raise ValueError("OHLC prices must be finite and positive")
    if ((values.high < values[["open", "close", "low"]].max(axis=1)) |
            (values.low > values[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError("Invalid OHLC price range")
    # Missing sessions, including whole missing days, must not become a longer
    # apparent holding bar. Use the same exchange calendar as execution.
    from app.services.exchange_sessions import is_nyse_session

    calendar_dates = pd.date_range(dates.iloc[0], dates.iloc[-1], freq="D")
    expected = []
    for day in calendar_dates:
        if is_nyse_session(day.date()):
            expected.append(day.date())
    if list(dates.dt.date) != expected:
        raise ValueError("Daily history has missing or non-session dates")
    return data, dates, values


def simulate_short_history(frame: pd.DataFrame, *, symbol: str, source: str,
                           config: ShortSimulationConfig | None = None) -> dict:
    """Next-open short research with restricted proceeds and modeled costs.

    Broker capabilities, borrow/dividends, and all-in costs remain unverified.
    Stops include adverse opening gaps and the entry session's high.
    """
    config = config or ShortSimulationConfig()
    data, dates, values = validated_short_history(frame, source=source, config=config)
    close = values.close.astype(float)
    rsi = wilder_rsi(close, 2)
    sma = close.rolling(config.trend_window).mean()
    fee_rate, slip = config.fee_bps / 10000, config.slippage_bps / 10000
    equity = config.starting_equity
    position = None
    trades, curve = [], []
    data["date"] = dates.dt.strftime("%Y-%m-%d")

    def cover(index: int, raw_price: float, reason: str) -> None:
        nonlocal equity, position
        price = raw_price * (1 + slip)
        days = (dates.iloc[index] - dates.iloc[position["entry_index"]]).days
        borrow = position["entry_price"] * position["quantity"] * config.annual_borrow_rate * days / 365
        exit_fee = price * position["quantity"] * fee_rate
        gross = (position["entry_price"] - price) * position["quantity"]
        pnl = gross - position["entry_fee"] - exit_fee - borrow
        equity += gross - exit_fee - borrow
        trades.append({**position, "exit_date": data.date.iloc[index], "exit_price": price,
                       "exit_reason": reason, "gross_pnl": gross, "net_pnl": pnl,
                       "exit_fee": exit_fee, "borrow_cost": borrow})
        position = None

    for index in range(config.trend_window, len(data)):
        bar = values.iloc[index]
        exited = False
        if position is not None:
            if bar.open >= position["stop_price"]:
                cover(index, float(bar.open), "stop_gap")
            elif index - position["entry_index"] >= config.max_holding_sessions:
                cover(index, float(bar.open), "time_stop")
            elif rsi.iloc[index - 1] <= config.exit_rsi:
                cover(index, float(bar.open), "rsi_cover")
            exited = position is None
        if position is None and not exited and equity > 0:
            # Only the previous completed session determines the entry.
            if close.iloc[index - 1] < sma.iloc[index - 1] and rsi.iloc[index - 1] > config.entry_rsi:
                price = float(bar.open) * (1 - slip)
                quantity = math.floor(equity * config.allocation / price)
                if quantity > 0:
                    entry_fee = quantity * price * fee_rate
                    equity -= entry_fee
                    position = {"entry_date": data.date.iloc[index], "entry_index": index,
                                "entry_price": price, "quantity": quantity,
                                "entry_fee": entry_fee, "stop_price": price * (1 + config.stop_loss_pct)}
        if position is not None and bar.high >= position["stop_price"]:
            cover(index, position["stop_price"], "stop_loss")
        if index == len(data) - 1 and position is not None:
            cover(index, float(bar.close), "end_of_history")
        unrealized = 0.0
        if position is not None:
            days = (dates.iloc[index] - dates.iloc[position["entry_index"]]).days
            unrealized = ((position["entry_price"] - float(bar.close)) * position["quantity"]
                          - position["entry_price"] * position["quantity"] * config.annual_borrow_rate * days / 365)
        curve.append({"date": data.date.iloc[index], "equity": equity + unrealized})
    fingerprint = hashlib.sha256(data.to_json(orient="split").encode()).hexdigest()
    run_id = hashlib.sha256(json.dumps({"dataset": fingerprint, "config": config.model_dump(),
                                       "symbol": symbol, "source": source, "version": 1}, sort_keys=True).encode()).hexdigest()
    peak, max_drawdown = config.starting_equity, 0.0
    for point in curve:
        peak = max(peak, point["equity"])
        max_drawdown = max(max_drawdown, (peak - point["equity"]) / peak)
    return {"run_id": run_id, "dataset_sha256": fingerprint, "strategy": "rsi2_short_bounce_v1",
            "symbol": symbol, "source": source, "mode": "historical_paper_simulation",
            "paper_only": True, "broker_orders_submitted": 0, "execution_eligible": False,
            "costs_verified": False, "config": config.model_dump(), "trades": trades,
            "equity_curve": curve, "ending_equity": equity, "net_pnl": equity - config.starting_equity,
            "max_drawdown": max_drawdown, "number_of_trades": len(trades),
            "limitations": ["Historical simulation, not Alpaca paper execution or a forward trial",
                            "Fees, slippage and borrow rates are assumptions, not verified costs",
                            "Borrow availability, recalls, dividends and corporate actions are not modeled",
                            "Not eligible for automatic promotion or live execution"]}
