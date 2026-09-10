"""Deterministic offline next-open, long-only simulation. No execution imports."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN

from app.services.instruments import utc_timestamp


def decimal(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not a numeric input")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError("Invalid numeric input") from exc
    if not result.is_finite():
        raise ValueError("Numeric input must be finite")
    return result


@dataclass(frozen=True)
class SimulationConfig:
    initial_cash: Decimal = Decimal("10000")
    allocation: Decimal = Decimal("1")
    fee_bps: Decimal = Decimal("10")
    spread_bps: Decimal = Decimal("2")
    slippage_bps: Decimal = Decimal("5")
    lot_step: Decimal = Decimal("0.000001")
    minimum_notional: Decimal = Decimal("1")
    max_volume_participation: Decimal = Decimal("0.01")

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, decimal(getattr(self, name)))
        if self.initial_cash <= 0 or self.lot_step <= 0 or self.minimum_notional < 0:
            raise ValueError("Invalid cash, lot step or minimum notional")
        if not 0 < self.allocation <= 1 or not 0 < self.max_volume_participation <= 1:
            raise ValueError("Allocation and participation must be in (0, 1]")
        if min(self.fee_bps, self.spread_bps, self.slippage_bps) < 0:
            raise ValueError("Costs cannot be negative")
        if self.fee_bps >= 10000 or self.spread_bps / 2 + self.slippage_bps >= 10000:
            raise ValueError("Costs must preserve positive execution proceeds")


def _bars(rows):
    result = []
    for row in rows:
        stamp = utc_timestamp(row.get("timestamp"))
        if result and stamp <= result[-1]["timestamp"]:
            raise ValueError("Bars must have strictly increasing unique timestamps")
        if type(row.get("tradable")) is not bool:
            raise ValueError("Calendar tradability must be explicit")
        values = {name: decimal(row[name]) for name in ("open", "high", "low", "close", "volume")}
        if min(values[n] for n in ("open", "high", "low", "close")) <= 0 or values["volume"] < 0:
            raise ValueError("Invalid price or volume")
        if values["high"] < max(values["open"], values["close"], values["low"]) or values["low"] > min(values["open"], values["close"], values["high"]):
            raise ValueError("Invalid OHLC bounds")
        result.append(values | {"timestamp": stamp, "tradable": row["tradable"]})
    if len(result) < 2:
        raise ValueError("At least two bars are required")
    return result


def _simulate(bars, decisions, config, *, buy_and_hold=False):
    cash, quantity = config.initial_cash, Decimal(0)
    costs, fees, spread_costs, slippage_costs = (Decimal(0) for _ in range(4))
    peak, max_drawdown = cash, Decimal(0)
    fills, equity = [], []
    completed_trades = 0
    buy_hold_target = None
    adverse = (config.spread_bps / 2 + config.slippage_bps) / 10000
    fee_rate = config.fee_bps / 10000
    def lot(value):
        return (value / config.lot_step).to_integral_value(rounding=ROUND_DOWN) * config.lot_step
    for index, bar in enumerate(bars):
        if index and bar["tradable"] and bar["volume"] > 0:
            # Only the preceding close's decision is available at this open.
            target = decisions[index - 1]
            side = "buy" if target == 1 else "sell"
            price = bar["open"] * (1 + adverse if side == "buy" else 1 - adverse)
            # Prior completed volume is an estimate available before this open.
            # Realized bar volume may reduce fills, never increase that estimate.
            capacity = lot(min(bars[index - 1]["volume"], bar["volume"]) * config.max_volume_participation)
            if side == "buy":
                current_equity = cash + quantity * bar["open"]
                remaining_notional = max(Decimal(0), current_equity * config.allocation - quantity * bar["open"])
                # Costs consume allocation capacity as well as available cash.
                requested = min(cash, remaining_notional) / (price * (1 + fee_rate))
                if buy_and_hold:
                    if buy_hold_target is None:
                        buy_hold_target = lot(config.initial_cash * config.allocation / (price * (1 + fee_rate)))
                    requested = min(max(Decimal(0), buy_hold_target - quantity), cash / (price * (1 + fee_rate)))
            else:
                requested = quantity
            filled = lot(min(requested, capacity))
            # Division uses Decimal context precision: never let a rounded-up
            # boundary lot consume more cash than is actually available.
            if side == "buy" and filled * price * (1 + fee_rate) > cash:
                filled = max(Decimal(0), filled - config.lot_step)
            notional = filled * price
            if filled > 0 and notional >= config.minimum_notional:
                fee = notional * fee_rate
                if side == "buy":
                    cash -= notional + fee
                    quantity += filled
                else:
                    cash += notional - fee
                    quantity -= filled
                    if quantity == 0:
                        completed_trades += 1
                spread = filled * bar["open"] * config.spread_bps / 20000
                slippage = filled * bar["open"] * config.slippage_bps / 10000
                fees += fee; spread_costs += spread; slippage_costs += slippage
                costs += fee + spread + slippage
                fills.append({"bar_index": index, "decision_index": index - 1, "timestamp": bar["timestamp"],
                              "side": side, "quantity": filled, "price": price, "notional": notional,
                              "fee": fee, "spread_cost": spread, "slippage_cost": slippage,
                              "partial": filled < lot(requested)})
        value = cash + quantity * bar["close"]
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, (peak - value) / peak)
        equity.append({"timestamp": bar["timestamp"], "cash": cash, "quantity": quantity, "equity": value})
    return {"cash": cash, "quantity": quantity, "equity": equity[-1]["equity"], "equity_curve": equity,
            "fills": fills, "fill_count": len(fills), "completed_trades": completed_trades,
            "total_costs": costs, "fees": fees, "spread_costs": spread_costs, "slippage_costs": slippage_costs,
            "total_return": equity[-1]["equity"] / config.initial_cash - 1, "max_drawdown": max_drawdown}


def simulate(rows: list[dict], decisions: list[int], config: SimulationConfig = SimulationConfig()) -> dict:
    bars = _bars(rows)
    if len(decisions) != len(bars) or any(type(value) is not int or value not in (0, 1) for value in decisions):
        raise ValueError("Exactly one binary integer decision per bar is required")
    result = _simulate(bars, decisions, config)
    result["benchmarks"] = {"cash": _simulate(bars, [0] * len(bars), config),
                            "buy_and_hold": _simulate(bars, [1] * len(bars), config, buy_and_hold=True)}
    result["mode"] = "offline_research"
    result["eligible_for_trading"] = False
    return result
