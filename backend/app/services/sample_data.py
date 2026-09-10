"""Test/demo-only generated prices. Never import into production research or execution."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd


def generate_sample_prices(symbol: str = "SPY", days: int = 260) -> pd.DataFrame:
    rng = np.random.default_rng(abs(hash(symbol)) % 2**32)
    dates = pd.bdate_range(end=date.today(), periods=days)
    drift = 0.00035
    volatility = 0.012
    shocks = rng.normal(drift, volatility, len(dates))
    close = 420 * np.exp(np.cumsum(shocks))
    open_ = close * (1 + rng.normal(0, 0.002, len(dates)))
    high = np.maximum(open_, close) * (1 + rng.uniform(0.001, 0.012, len(dates)))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.001, 0.012, len(dates)))
    volume = rng.integers(55_000_000, 120_000_000, len(dates))
    return pd.DataFrame(
        {
            "date": dates.date,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "adjusted_close": close,
            "volume": volume,
        }
    )


def demo_equity_curve() -> list[dict]:
    prices = generate_sample_prices("EQUITY", 90)
    returns = prices["close"].pct_change().fillna(0) * 0.55
    equity = 100_000 * (1 + returns).cumprod()
    return [{"date": str(row.date), "value": round(value, 2)} for row, value in zip(prices.itertuples(), equity)]
