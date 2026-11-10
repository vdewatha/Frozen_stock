"""The two gate readings every paper signal records at decision time.

The frozen forward experiments (``reports/local-sweep-20261110/forward-experiments.md``,
``forward_experiments.py``) tag each paper decision with the market state the
sweep gated on, so the review can split a candidate's trades by the reading
without re-deriving it from history later:

* ``vix_above_20``: the CBOE VIX close dated on the decision bar is >= 20
  (``strategies/vix_gate.vix_reading``: the close dated on or before the bar,
  unavailable when older than ``MAX_STALE_DAYS`` = 5 sessions).  The gated
  strategies fail open on a missing close; the *reading* is recorded as
  ``None`` so the review never guesses a bucket.
* ``spy_sma50``: SPY's last completed close is above the mean of its last 50
  completed closes (``market_gate.market_state`` on the cycle's trusted SPY
  frame; strict ``>``, as the market gate decides).

The readings are stored under ``features["gate_readings"]`` of every signal
the scheduled cycle or the manual signal endpoint persists (BUY, HOLD and
SELL alike), beside the strategy's own features.  They change no decision.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

import pandas as pd

logger = logging.getLogger(__name__)

FEATURE_KEY = "gate_readings"
VIX_LEVEL = 20.0
SPY_WINDOW = 50
GATES = ("vix_above_20", "spy_sma50")


def decision_bar(prices: Any) -> Optional[str]:
    """ISO date of the last completed bar of a daily frame (the bar the decision is made on)."""
    from app.services.strategies.vix_gate import _bar_date

    if not isinstance(prices, pd.DataFrame):
        return None
    try:
        return _bar_date(prices)
    except Exception:
        return None


def vix_above_20(reading: Mapping, level: float = VIX_LEVEL) -> dict:
    """``{"value": True/False/None, "vix", "vix_date", "reason"}`` from a ``vix_gate.vix_reading`` dict."""
    vix = reading.get("vix")
    if vix is None:
        return {"value": None, "vix": None, "vix_date": reading.get("vix_date"), "level": level,
                "reason": reading.get("reason") or "vix_unavailable"}
    return {"value": bool(float(vix) >= level), "vix": round(float(vix), 2), "vix_date": reading.get("vix_date"),
            "level": level, "reason": None}


def spy_sma50(spy_frame: Any, window: int = SPY_WINDOW) -> dict:
    """``{"value": True/False/None, "close", "sma", "bar", "reason"}`` from SPY's completed daily bars."""
    from app.services.market_gate import market_state

    if spy_frame is None:
        return {"value": None, "close": None, "sma": None, "bar": None, "window": window,
                "reason": "spy_unavailable"}
    try:
        state = market_state(spy_frame, window)
    except Exception as exc:
        return {"value": None, "close": None, "sma": None, "bar": None, "window": window,
                "reason": f"spy_unreadable: {exc.__class__.__name__}"}
    if "close" not in state:  # shorter than the window: the gate fails closed, the reading is unknown
        return {"value": None, "close": None, "sma": None, "bar": None, "window": window,
                "reason": state.get("reason")}
    return {"value": bool(state["allowed"]), "close": state.get("close"), "sma": state.get("sma"),
            "bar": state.get("bar"), "window": window, "reason": None}


def gate_readings(prices: Any, spy_frame: Any, *, vix: Optional[Mapping] = None) -> dict:
    """Both readings for a decision on the last bar of ``prices`` (flat, JSON-safe)."""
    bar = decision_bar(prices)
    if vix is None:
        from app.services.strategies.vix_gate import vix_reading

        try:
            vix = vix_reading(bar) if bar else {"vix": None, "vix_date": None, "reason": "vix_unavailable"}
        except Exception as exc:  # a reading only; never changes or blocks the signal
            logger.info("gate readings: VIX unavailable: %s", exc.__class__.__name__)
            vix = {"vix": None, "vix_date": None, "reason": "vix_unavailable"}
    v = vix_above_20(vix)
    s = spy_sma50(spy_frame)
    return {
        "decision_bar": bar,
        "vix_above_20": v["value"], "vix": v["vix"], "vix_date": v["vix_date"], "vix_level": v["level"],
        "vix_reason": v["reason"],
        "spy_sma50": s["value"], "spy_close": s["close"], "spy_sma": s["sma"], "spy_bar": s["bar"],
        "spy_window": s["window"], "spy_reason": s["reason"],
    }


class GateReadingContext:
    """Per-cycle cache: SPY's state once, the VIX reading once per decision bar."""

    def __init__(self, load_spy: Callable[[], Any]) -> None:
        self._load_spy = load_spy
        self._spy_state: Optional[dict] = None
        self._by_bar: dict[Optional[str], dict] = {}

    def spy_state(self) -> dict:
        if self._spy_state is None:
            try:
                frame = self._load_spy()
            except Exception as exc:
                logger.info("gate readings: SPY unavailable: %s", exc.__class__.__name__)
                frame = None
            self._spy_state = spy_sma50(frame)
        return self._spy_state

    def for_prices(self, prices: Any) -> dict:
        bar = decision_bar(prices)
        if bar not in self._by_bar:
            from app.services.strategies.vix_gate import vix_reading

            try:
                reading = vix_reading(bar) if bar else {"vix": None, "vix_date": None, "reason": "vix_unavailable"}
            except Exception as exc:
                logger.info("gate readings: VIX unavailable: %s", exc.__class__.__name__)
                reading = {"vix": None, "vix_date": None, "reason": "vix_unavailable"}
            v, s = vix_above_20(reading), self.spy_state()
            self._by_bar[bar] = {
                "decision_bar": bar,
                "vix_above_20": v["value"], "vix": v["vix"], "vix_date": v["vix_date"], "vix_level": v["level"],
                "vix_reason": v["reason"],
                "spy_sma50": s["value"], "spy_close": s["close"], "spy_sma": s["sma"], "spy_bar": s["bar"],
                "spy_window": s["window"], "spy_reason": s["reason"],
            }
        return dict(self._by_bar[bar])


def readings_of(features: Optional[Mapping]) -> Optional[dict]:
    """The recorded readings on a signal's features, or None."""
    block = (features or {}).get(FEATURE_KEY)
    return dict(block) if isinstance(block, Mapping) else None
