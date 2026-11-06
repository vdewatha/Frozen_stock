"""Market-direction gate: no new longs while SPY is below its N-day average.

The loss diagnosis (``loss_diagnosis.py``) found that most paper losses in the
first weeks were ``missed_market_move``: the whole market fell and took the
position with it, often several stops on the same day.  This gate looks at the
market, not the traded symbol: a strategy row that sets
``parameters["market_gate_sma"] = N`` (N > 0) only buys while SPY's last
completed close is above the simple average of its last N completed closes.

* It only withholds new entries.  Sells, protective exits and broker legs are
  never consulted (it runs inside ``entry_protections``).
* Missing or too-short SPY history fails closed for gated strategies.
* The backtest (``scripts/run_market_gate_backtest.py``) and the paper path
  use the same ``spy_above_sma`` on completed daily bars, decided on the close
  of bar t and filled at the next open.

Which strategies are gated, and with which N, comes from
``reports/market-gate-20261106.md`` (``MARKET_GATED_STRATEGIES`` in seed.py).
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

MARKET_GATE_KEY = "market_gate_sma"
MARKET_SYMBOL = "SPY"


def gate_window(parameters: Optional[dict]) -> int:
    """The configured SMA length; 0 (off) for a missing, invalid or non-positive value."""
    try:
        value = int((parameters or {}).get(MARKET_GATE_KEY) or 0)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def spy_above_sma(close: pd.Series, window: int) -> np.ndarray:
    """Per bar: close above the mean of the last ``window`` closes (inclusive). False until defined."""
    close = pd.to_numeric(pd.Series(close), errors="coerce").astype(float).reset_index(drop=True)
    sma = close.rolling(window, min_periods=window).mean().to_numpy()
    values = close.to_numpy()
    return np.isfinite(sma) & np.isfinite(values) & (values > sma)


def market_state(spy_frame: Optional[pd.DataFrame], window: int) -> dict:
    """Gate state from the last completed SPY bar of ``spy_frame``."""
    if window <= 0:
        return {"allowed": True, "window": 0, "reason": "market gate off"}
    if spy_frame is None or len(spy_frame) < window or "close" not in spy_frame:
        return {"allowed": False, "window": window,
                "reason": f"{MARKET_SYMBOL} history is shorter than {window} bars (fail closed)"}
    close = pd.to_numeric(spy_frame["close"], errors="coerce").astype(float).reset_index(drop=True)
    above = bool(spy_above_sma(close, window)[-1])
    sma = float(close.iloc[-window:].mean())
    last = float(close.iloc[-1])
    bar = str(spy_frame["date"].iloc[-1])[:10] if "date" in spy_frame else None
    return {"allowed": above, "window": window, "close": round(last, 4), "sma": round(sma, 4), "bar": bar,
            "reason": (f"{MARKET_SYMBOL} {last:.2f} {'above' if above else 'at or below'} its "
                       f"{window}-day average {sma:.2f}")}
