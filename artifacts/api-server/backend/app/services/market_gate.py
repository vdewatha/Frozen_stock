"""Trend gates on a benchmark: no new longs while SPY (or the symbol's sector ETF) is below its N-day average.

The loss diagnosis (``loss_diagnosis.py``) found that most paper losses in the
first weeks were ``missed_market_move``: the whole market fell and took the
position with it, often several stops on the same day.  The *market gate*
looks at the market, not the traded symbol: a strategy row that sets
``parameters["market_gate_sma"] = N`` (N > 0) only buys while SPY's last
completed close is above the simple average of its last N completed closes.

The same rule generalises to a symbol's sector.  ``missed_sector_move`` is the
sibling lesson (the sector ETF fell and the stock fell with it): a row with
``parameters["sector_gate_sma"] = N`` takes no new entry in a symbol while that
symbol's sector ETF (``sector_symbol``: the SPDR ETF ``trading_universe
.sector_group`` maps it to, the same mapping the loss diagnosis attributes
against) is at or below its N-day average.  Index ETFs, the sector ETFs
themselves and unmapped symbols have no sector ETF and are never sector-gated.

* Both gates only withhold new entries.  Sells, protective exits and broker
  legs are never consulted (they run inside ``entry_protections``).
* Missing or too-short benchmark history fails closed for gated strategies.
* The backtests (``scripts/run_market_gate_backtest.py``, ``gate_replay``)
  and the paper path use the same ``above_sma`` on completed daily bars,
  decided on the close of bar t and filled at the next open.

Which strategies are market-gated, and with which N, comes from
``reports/market-gate-20261106.md`` (``MARKET_GATED_STRATEGIES`` in seed.py);
sector gates are adopted only by the experiment engine (``sector_gate``
template) until a report says otherwise.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

MARKET_GATE_KEY = "market_gate_sma"
SECTOR_GATE_KEY = "sector_gate_sma"
MARKET_SYMBOL = "SPY"
# Groups of ``trading_universe.SECTOR_GROUPS`` that are not a sector ETF.
NON_SECTOR_GROUPS = frozenset({"index", "inverse"})


def _window(parameters: Optional[dict], key: str) -> int:
    try:
        value = int((parameters or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def gate_window(parameters: Optional[dict]) -> int:
    """The configured market-gate SMA length; 0 (off) for a missing, invalid or non-positive value."""
    return _window(parameters, MARKET_GATE_KEY)


def sector_gate_window(parameters: Optional[dict]) -> int:
    """The configured sector-gate SMA length; 0 (off) for a missing, invalid or non-positive value."""
    return _window(parameters, SECTOR_GATE_KEY)


def sector_symbol(symbol: str) -> Optional[str]:
    """The sector ETF a symbol's entries are gated on; ``None`` for index ETFs, the ETFs themselves, unmapped names."""
    from app.services.trading_universe import sector_group

    symbol = str(symbol).strip().upper()
    group = sector_group(symbol)
    if group in NON_SECTOR_GROUPS or group == symbol:
        return None
    return group


def above_sma(close: pd.Series, window: int) -> np.ndarray:
    """Per bar: close above the mean of the last ``window`` closes (inclusive). False until defined."""
    close = pd.to_numeric(pd.Series(close), errors="coerce").astype(float).reset_index(drop=True)
    sma = close.rolling(window, min_periods=window).mean().to_numpy()
    values = close.to_numpy()
    return np.isfinite(sma) & np.isfinite(values) & (values > sma)


# The market gate's original name; ``gate_replay`` and the backtest script use it.
spy_above_sma = above_sma


def gate_state(frame: Optional[pd.DataFrame], window: int, symbol: str = MARKET_SYMBOL) -> dict:
    """Gate state from the last completed bar of ``frame`` (the benchmark ``symbol``'s daily history)."""
    if window <= 0:
        return {"allowed": True, "window": 0, "symbol": symbol, "reason": f"{symbol} gate off"}
    if frame is None or len(frame) < window or "close" not in frame:
        return {"allowed": False, "window": window, "symbol": symbol,
                "reason": f"{symbol} history is shorter than {window} bars (fail closed)"}
    close = pd.to_numeric(frame["close"], errors="coerce").astype(float).reset_index(drop=True)
    above = bool(above_sma(close, window)[-1])
    sma = float(close.iloc[-window:].mean())
    last = float(close.iloc[-1])
    bar = str(frame["date"].iloc[-1])[:10] if "date" in frame else None
    return {"allowed": above, "window": window, "symbol": symbol, "close": round(last, 4), "sma": round(sma, 4),
            "bar": bar,
            "reason": (f"{symbol} {last:.2f} {'above' if above else 'at or below'} its "
                       f"{window}-day average {sma:.2f}")}


def market_state(spy_frame: Optional[pd.DataFrame], window: int) -> dict:
    """Gate state from the last completed SPY bar of ``spy_frame``."""
    state = gate_state(spy_frame, window, MARKET_SYMBOL)
    if window <= 0:
        state["reason"] = "market gate off"
    return state


def sector_state(sector_frame: Optional[pd.DataFrame], window: int, etf: str) -> dict:
    """Gate state from the last completed bar of the sector ETF ``etf``."""
    state = gate_state(sector_frame, window, etf)
    if window <= 0:
        state["reason"] = "sector gate off"
    return state
