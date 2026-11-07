"""Position-sizing modes for the paper book (pure functions; paper only).

Expectancy turns into profit through size, and until now the size of every
scheduled BUY was one rule: ``max_risk_per_trade`` of equity, times the
strategy's bounded allocation weight (``strategy_allocation``), times the
volatility / market-gauge scale.  That rule stays the baseline and the *cap*:
no mode here can size a position above what the fixed fraction would have,
so every existing limit (the ledger's per-trade exposure check, the sector
and position caps, the one-share floor, learning-sized candidates) keeps
applying unchanged.

Modes (a strategy row's ``sizing_mode`` parameter; ``risk_per_trade`` is the
mode's risk budget as a fraction of equity):

* ``fixed_fraction``: today's rule, the baseline.
* ``volatility_target``: the notional at which the loss at the strategy's
  stop equals ``risk_per_trade`` of equity (default 0.5%), using the stop
  distance of the strategy's exit policy; capped at the fixed-fraction
  notional.  Without a usable stop distance it falls back to the fixed
  fraction.
* ``fractional_kelly``: quarter Kelly from the meta-label's calibrated
  probability for the signal and the strategy's empirical payoff ratio
  (mean win / mean loss of its closed paper round trips).  Used only when
  ``meta_label_modes`` reports the strategy in enforce mode (the probability
  is then calibrated *and* decides entries), the signal carries a
  probability, and at least ``KELLY_MIN_TRADES`` closed trades exist with
  both wins and losses; otherwise it falls back to the volatility target.
  A non-positive Kelly fraction (no edge at that probability and payoff)
  also falls back rather than sizing zero, so the book keeps learning.
  Capped at the fixed-fraction notional.

The autotrader records the mode, its inputs and the resulting notional on
the signal's features (``features.sizing``) so attribution can see them.
Live trading never reads any of this.
"""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Any, Iterable, Mapping, Optional

FIXED_FRACTION = "fixed_fraction"
VOLATILITY_TARGET = "volatility_target"
FRACTIONAL_KELLY = "fractional_kelly"
MODES = (FIXED_FRACTION, VOLATILITY_TARGET, FRACTIONAL_KELLY)
DEFAULT_MODE = FIXED_FRACTION
DEFAULT_RISK_PER_TRADE = 0.005
MAX_RISK_PER_TRADE = 0.05
KELLY_FRACTION = 0.25
KELLY_MIN_TRADES = 30
ZERO = Decimal("0")
ONE = Decimal("1")
NOTIONAL_INCREMENT = Decimal("0.00000001")


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _positive_fraction(value: Any) -> Optional[float]:
    """``value`` as a float strictly inside (0, 1), else None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or not 0 < number < 1:  # NaN or out of range
        return None
    return number


def sizing_parameters(parameters: Optional[Mapping]) -> dict:
    """The sizing mode and risk budget a strategy row trades with (invalid values = the defaults)."""
    params = dict(parameters or {})
    mode = str(params.get("sizing_mode") or DEFAULT_MODE).strip().lower()
    if mode not in MODES:
        mode = DEFAULT_MODE
    risk = _positive_fraction(params.get("risk_per_trade"))
    if risk is None or risk > MAX_RISK_PER_TRADE:
        risk = DEFAULT_RISK_PER_TRADE
    return {"sizing_mode": mode, "risk_per_trade": risk}


# ----------------------------------------------------------------- the modes

def fixed_fraction(equity: Any, risk_fraction: Any, size_scale: Any = ONE) -> dict:
    """Today's rule: ``equity x risk_fraction x size_scale`` with the scale clamped to [0, 1]."""
    scale = min(max(_decimal(size_scale), ZERO), ONE)
    notional = max(ZERO, _decimal(equity)) * _decimal(risk_fraction) * scale
    return {"mode": FIXED_FRACTION, "status": "sized", "notional": notional.quantize(NOTIONAL_INCREMENT, ROUND_DOWN),
            "risk_fraction": str(_decimal(risk_fraction)), "size_scale": str(scale)}


def volatility_target(equity: Any, stop_distance: Any, risk_per_trade: Any, cap_notional: Any) -> dict:
    """Notional whose loss at the stop is ``risk_per_trade`` of equity, never above ``cap_notional``."""
    cap = max(ZERO, _decimal(cap_notional))
    stop = _positive_fraction(stop_distance)
    risk = _positive_fraction(risk_per_trade)
    base = {"mode": VOLATILITY_TARGET, "stop_distance": stop, "risk_per_trade": risk, "cap_notional": cap}
    if stop is None or risk is None:
        return base | {"status": "fallback_fixed_fraction", "notional": cap,
                       "reason": ("no stop distance: the strategy's exit policy has no stop leg"
                                  if stop is None else "no valid risk_per_trade")}
    target = (max(ZERO, _decimal(equity)) * Decimal(str(risk)) / Decimal(str(stop))).quantize(NOTIONAL_INCREMENT,
                                                                                               ROUND_DOWN)
    capped = target > cap
    return base | {"status": "capped" if capped else "sized", "notional": min(target, cap), "target_notional": target,
                   "capped": capped}


def kelly_fraction(probability: Any, payoff_ratio: Any, fraction: float = KELLY_FRACTION) -> dict:
    """Full Kelly ``p - (1 - p) / b`` and its fraction; both floored at zero."""
    p = float(probability)
    b = float(payoff_ratio)
    if not 0 <= p <= 1 or b <= 0:
        raise ValueError("kelly_fraction needs a probability in [0, 1] and a positive payoff ratio")
    full = p - (1 - p) / b
    return {"probability": p, "payoff_ratio": b, "full_kelly": full, "fraction": fraction,
            "kelly": max(0.0, full * fraction)}


def payoff_ratio(returns: Iterable[Any]) -> dict:
    """Mean win over mean loss of per-trade net returns (a scratch after costs is a loss)."""
    values = []
    for value in returns:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number:
            values.append(number)
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v <= 0]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = -sum(losses) / len(losses) if losses else None
    ratio = avg_win / avg_loss if avg_win is not None and avg_loss else None
    return {"trades": len(values), "wins": len(wins), "losses": len(losses),
            "avg_win": round(avg_win, 8) if avg_win is not None else None,
            "avg_loss": round(avg_loss, 8) if avg_loss is not None else None,
            "payoff_ratio": round(ratio, 6) if ratio is not None else None}


def fractional_kelly(equity: Any, probability: Any, payoff: Mapping, *, enforced: bool, cap_notional: Any,
                     fallback: Mapping, fraction: float = KELLY_FRACTION,
                     min_trades: int = KELLY_MIN_TRADES) -> dict:
    """Quarter Kelly of equity from a calibrated probability and the empirical payoff, capped; else ``fallback``."""
    cap = max(ZERO, _decimal(cap_notional))
    trades = int(payoff.get("trades") or 0)
    ratio = payoff.get("payoff_ratio")
    base = {"mode": FRACTIONAL_KELLY, "probability": None if probability is None else float(probability),
            "payoff_ratio": ratio, "closed_trades": trades, "kelly_fraction": fraction, "cap_notional": cap}

    def fall_back(reason: str) -> dict:
        return base | {"status": "fallback_" + str(fallback.get("mode", VOLATILITY_TARGET)), "reason": reason,
                       "notional": min(cap, max(ZERO, _decimal(fallback.get("notional", cap)))),
                       "fallback": {key: value for key, value in fallback.items() if key != "cap_notional"}}

    if not enforced:
        return fall_back("meta-label is not in enforce mode for this strategy")
    if probability is None:
        return fall_back("the signal carries no calibrated probability")
    if trades < min_trades:
        return fall_back(f"{trades} closed paper trades, fewer than {min_trades}")
    if ratio is None or float(ratio) <= 0:
        return fall_back("the closed trades have no measurable payoff ratio (wins and losses both needed)")
    try:
        kelly = kelly_fraction(probability, ratio, fraction)
    except ValueError as exc:
        return fall_back(str(exc))
    if kelly["kelly"] <= 0:
        return fall_back(f"quarter Kelly is not positive (full Kelly {kelly['full_kelly']:.4f})")
    target = (max(ZERO, _decimal(equity)) * Decimal(str(kelly["kelly"]))).quantize(NOTIONAL_INCREMENT, ROUND_DOWN)
    capped = target > cap
    return base | {"status": "capped" if capped else "sized", "notional": min(target, cap), "target_notional": target,
                   "capped": capped, "full_kelly": round(kelly["full_kelly"], 6), "kelly": round(kelly["kelly"], 6)}


def size_notional(*, mode: str, equity: Any, risk_fraction: Any, size_scale: Any = ONE,
                  stop_distance: Any = None, risk_per_trade: Any = DEFAULT_RISK_PER_TRADE,
                  probability: Any = None, payoff: Optional[Mapping] = None, enforced: bool = False,
                  cap_notional: Any = None) -> dict:
    """The notional for one BUY under ``mode``; the fixed fraction is always the cap.

    ``risk_fraction`` and ``size_scale`` are today's inputs (strategy risk
    fraction times allocation weight; volatility and gauge scale), so
    ``fixed_fraction`` reproduces the live rule exactly.  A caller that has
    already computed the fixed-fraction notional passes it as ``cap_notional``.
    """
    fixed = fixed_fraction(equity, risk_fraction, size_scale)
    if cap_notional is not None:
        fixed["notional"] = max(ZERO, _decimal(cap_notional)).quantize(NOTIONAL_INCREMENT, ROUND_DOWN)
    cap = fixed["notional"]
    if mode not in MODES:
        mode = DEFAULT_MODE
    if mode == FIXED_FRACTION:
        out = dict(fixed)
    elif mode == VOLATILITY_TARGET:
        out = volatility_target(equity, stop_distance, risk_per_trade, cap)
    else:
        fallback = volatility_target(equity, stop_distance, risk_per_trade, cap)
        out = fractional_kelly(equity, probability, payoff or {"trades": 0, "payoff_ratio": None},
                               enforced=enforced, cap_notional=cap, fallback=fallback)
    out["requested_mode"] = mode
    out["fixed_fraction_notional"] = cap
    out["notional"] = min(cap, max(ZERO, _decimal(out["notional"])))
    return out


def sizing_features(sized: Mapping, *, quantity: Any, reference_price: Any) -> dict:
    """JSON-safe record of how a BUY was sized (``features.sizing`` on the signal)."""
    out: dict = {}
    for key, value in sized.items():
        if key == "fallback" and isinstance(value, Mapping):
            out[key] = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in value.items()}
        elif isinstance(value, Decimal):
            out[key] = str(value)
        else:
            out[key] = value
    out["quantity"] = str(_decimal(quantity))
    out["reference_price"] = str(_decimal(reference_price))
    out["paper_only"] = True
    return out


# ----------------------------------------------------------------- evidence

def strategy_payoff(db, strategy_type: str) -> dict:
    """Empirical payoff ratio of the strategy's closed scheduled paper round trips (costs known)."""
    from app.services.trade_attribution import current_attributions

    rows = current_attributions(db, sources=["scheduled_round_trip"], strategy_types=[strategy_type])
    returns = [row.net_return for row in rows if row.costs_known and row.net_return is not None]
    return payoff_ratio(returns) | {"strategy_type": strategy_type, "source": "scheduled_round_trip"}
