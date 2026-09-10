"""Pure, versioned feature generators shared by training and inference.

Architecture inspired by asavinov/intelligent-trading-bot's configurable
generator dispatch (MIT). This is an independent implementation; no upstream
code, runtime, dynamic imports, or trading side effects are included.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from types import MappingProxyType

import numpy as np
import pandas as pd

PIPELINE_VERSION = "1"


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    generator: str
    window: int = 20


def _rsi(frame: pd.DataFrame, window: int) -> pd.Series:
    delta = frame.close.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = -delta.clip(upper=0).rolling(window).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))


def _macd(frame: pd.DataFrame, window: int) -> pd.Series:
    macd = frame.close.ewm(span=12, adjust=False).mean() - frame.close.ewm(span=26, adjust=False).mean()
    return macd - macd.ewm(span=9, adjust=False).mean()


GENERATORS = MappingProxyType({
    "return": lambda f, w: f.close.pct_change(w, fill_method=None),
    "volatility": lambda f, w: f.close.pct_change(fill_method=None).rolling(w).std(),
    "volume_change": lambda f, w: f.volume.pct_change(w, fill_method=None),
    "rsi": _rsi,
    "macd_histogram": _macd,
    "ma_distance_20_50": lambda f, w: (f.close.rolling(20).mean() - f.close.rolling(50).mean()) / f.close,
    "close_to_high": lambda f, w: f.close / f.close.rolling(w).max() - 1,
    "close_to_low": lambda f, w: f.close / f.close.rolling(w).min() - 1,
})

DEFAULT_FEATURES = (
    FeatureSpec("return_5d", "return", 5),
    FeatureSpec("return_20d", "return", 20),
    FeatureSpec("return_50d", "return", 50),
    FeatureSpec("volatility_20d", "volatility", 20),
    FeatureSpec("volume_change_20d", "volume_change", 20),
    FeatureSpec("rsi_14", "rsi", 14),
    FeatureSpec("macd_histogram", "macd_histogram"),
    FeatureSpec("ma_distance_20_50", "ma_distance_20_50"),
    FeatureSpec("close_to_high_20d", "close_to_high", 20),
    FeatureSpec("close_to_low_20d", "close_to_low", 20),
)


def feature_config_id(specs: tuple[FeatureSpec, ...] = DEFAULT_FEATURES, *, version: str = "1") -> str:
    if version not in {"1", "2"}:
        raise ValueError("Unsupported feature version")
    payload = {"version": PIPELINE_VERSION, "features": [asdict(s) for s in specs], "clip": [-5, 5]}
    if version == "2":
        payload = {"version": "2", "features": [asdict(s) for s in specs],
                   "scaling": "rsi_centered_50_macd_div_close_no_global_clip",
                   "rsi_zero_loss": "100_if_gain_else_50"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def generate_features(prices: pd.DataFrame, specs: tuple[FeatureSpec, ...] = DEFAULT_FEATURES, *, version: str = "1") -> pd.DataFrame:
    """Generate causal features; never fill missing prices or create labels.

    Windows count observations, matching the existing daily-model convention.
    Input must describe one instrument. Output preserves legacy clipping.
    """
    config_id = feature_config_id(specs, version=version)
    if not {"date", "close", "volume"}.issubset(prices.columns):
        raise ValueError("Features require date, close and volume columns")
    frame = prices.copy().sort_values("date").reset_index(drop=True)
    if frame.date.isna().any() or frame.date.duplicated().any():
        raise ValueError("Feature dates must be unique and non-null")
    names = [s.name for s in specs]
    if not specs or len(set(names)) != len(names) or any(n in frame.columns for n in names):
        raise ValueError("Feature names must be unique and cannot overwrite input columns")
    for s in specs:
        if s.generator not in GENERATORS or type(s.window) is not int or s.window < 1:
            raise ValueError(f"Invalid feature generator or window: {s}")
    for column in ("close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    # Each generator receives only original observations, never other features.
    original = frame.copy()
    for s in specs:
        values = GENERATORS[s.generator](original, s.window)
        if version == "2":
            if s.generator == "rsi":
                delta = original.close.diff()
                gain = delta.clip(lower=0).rolling(s.window).mean()
                loss = -delta.clip(upper=0).rolling(s.window).mean()
                values = values.mask((loss == 0) & (gain > 0), 100).mask((loss == 0) & (gain == 0), 50)
                values = (values - 50) / 50
            elif s.generator == "macd_histogram":
                values = values / original.close
        values = values.replace([np.inf, -np.inf], np.nan).astype(float)
        frame[s.name] = values.clip(-5, 5) if version == "1" else values
    frame.attrs["feature_config_id"] = config_id
    frame.attrs["feature_pipeline_version"] = version
    return frame
