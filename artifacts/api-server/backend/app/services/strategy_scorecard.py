"""Strategy scorecard: win rate *and* expectancy, per strategy x symbol x window.

The owner's target is "profitable 8 out of 10 trades while still making money
after costs". A win rate alone is easy to manufacture (tight target, wide or no
stop), so every number here is reported next to the expectancy that pays for
it (see reports/open-source-research-20261028.md, section 0):

    expectancy per trade = p * W - (1 - p) * L      (net of all costs)
    break-even win rate  = L / (W + L)

Every (strategy variant, symbol) pair is run through the shared honest
backtester (``app.services.backtester.run_backtest``: decisions on completed
bars, next-open fills, intrabar stop-first exits, volatility-scaled costs).
Trade returns are the backtester's ``profit_loss_pct`` (net of fees, spread,
slippage and impact on both sides). Results are sliced into evaluation
windows by trade *entry* date; equity statistics are re-anchored at the first
bar of each window.

Statistical honesty:

* Wilson interval and a cluster bootstrap for win rate and expectancy. Trades
  are resampled in calendar-month clusters, so trades that overlap in time
  across symbols (one market shock hitting 40 names) are not treated as
  independent evidence.
* The deflated Sharpe ratio (Bailey & Lopez de Prado, 2014) counts every
  parameter variant as a trial: pooled results are deflated over all variants
  run in that window, per-symbol results over variants x symbols (choosing
  "the symbols where it works" is also a selection).

Pure computation only: no database, broker or order code is imported.
"""
from __future__ import annotations

import math
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from statistics import NormalDist
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from app.services.backtest_statistics import deflated_sharpe_ratio, return_moments
from app.services.backtester import BacktestConfig, _buy_and_hold_values, run_backtest

# Bars the backtester skips before its first decision (``min_history + 1``).
FIRST_EVALUATED_INDEX = 61

GOAL_WIN_RATE = 0.80
MIN_POOLED_TRADES = 100
MIN_SYMBOL_TRADES = 20
GOAL_PROFIT_FACTOR = 1.3

DEFAULT_WINDOWS: dict[str, Optional[str]] = {
    "all": None,
    "post2015": "2015-01-01",
    "post2020": "2020-01-01",
}


# --------------------------------------------------------------------------
# Trade-level statistics
# --------------------------------------------------------------------------

def wilson_interval(successes: int, trials: int, confidence: float = 0.95) -> Optional[list[float]]:
    if trials <= 0:
        return None
    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _cluster_bootstrap(returns: np.ndarray, clusters: Sequence, samples: int, seed: int,
                       confidence: float) -> dict:
    """Resample whole clusters; return CIs for win rate and mean return."""
    keys, inverse = np.unique(np.asarray([str(value) for value in clusters]), return_inverse=True)
    k = len(keys)
    sums = np.bincount(inverse, weights=returns, minlength=k)
    counts = np.bincount(inverse, minlength=k).astype(float)
    wins = np.bincount(inverse, weights=(returns > 0).astype(float), minlength=k)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, k, size=(samples, k))
    total = counts[draws].sum(axis=1)
    total = np.where(total > 0, total, np.nan)
    means = sums[draws].sum(axis=1) / total
    win_rates = wins[draws].sum(axis=1) / total
    tail = (1 - confidence) / 2 * 100
    means, win_rates = means[np.isfinite(means)], win_rates[np.isfinite(win_rates)]
    return {
        "expectancy_ci": [float(v) for v in np.percentile(means, [tail, 100 - tail])],
        "win_rate_ci": [float(v) for v in np.percentile(win_rates, [tail, 100 - tail])],
        "clusters": int(k),
    }


def trade_statistics(returns: Iterable[float], *, clusters: Optional[Sequence] = None,
                     samples: int = 2000, seed: int = 7, confidence: float = 0.95,
                     min_bootstrap_trades: int = 10) -> dict:
    """Win rate, payoff and expectancy of per-trade net returns (fractions).

    A trade is a win when its net return is > 0 (the backtester's rule: a
    scratch after costs is a loss). ``avg_loss`` is reported as a positive
    magnitude. ``clusters`` (one label per trade) drives the bootstrap; the
    default treats every trade as its own cluster (iid bootstrap).
    """
    data = np.asarray([float(value) for value in returns], dtype=float)
    data = data[np.isfinite(data)]
    n = int(data.size)
    result: dict = {"trades": n}
    if n == 0:
        result.update({"wins": 0, "losses": 0, "win_rate": None, "win_rate_wilson_ci": None,
                       "avg_win_pct": None, "avg_loss_pct": None, "payoff_ratio": None,
                       "expectancy_pct": None, "expectancy_bps": None, "profit_factor": None,
                       "breakeven_win_rate": None, "median_pct": None, "worst_pct": None, "best_pct": None,
                       "win_rate_ci": None, "expectancy_ci_pct": None, "expectancy_ci_bps": None,
                       "bootstrap": {"status": "no_trades"}})
        return result
    wins = data[data > 0]
    losses = data[data <= 0]
    win_rate = len(wins) / n
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(-losses.mean()) if len(losses) else 0.0
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    expectancy = float(data.mean())
    result.update({
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate": round(win_rate, 4),
        "win_rate_wilson_ci": wilson_interval(len(wins), n, confidence),
        "avg_win_pct": round(avg_win * 100, 4),
        "avg_loss_pct": round(avg_loss * 100, 4),
        "payoff_ratio": round(avg_win / avg_loss, 4) if avg_loss > 0 else None,
        "expectancy_pct": round(expectancy * 100, 4),
        "expectancy_bps": round(expectancy * 10_000, 2),
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "breakeven_win_rate": round(avg_loss / (avg_win + avg_loss), 4) if (avg_win + avg_loss) > 0 else None,
        "median_pct": round(float(np.median(data)) * 100, 4),
        "worst_pct": round(float(data.min()) * 100, 4),
        "best_pct": round(float(data.max()) * 100, 4),
    })
    if n < min_bootstrap_trades:
        result.update({"win_rate_ci": None, "expectancy_ci_pct": None, "expectancy_ci_bps": None,
                       "bootstrap": {"status": "insufficient_trades", "minimum_trades": min_bootstrap_trades}})
        return result
    labels = list(clusters) if clusters is not None else list(range(n))
    if len(labels) != n:
        raise ValueError("clusters must have one label per finite trade return")
    boot = _cluster_bootstrap(data, labels, samples, seed, confidence)
    result.update({
        "win_rate_ci": [round(v, 4) for v in boot["win_rate_ci"]],
        "expectancy_ci_pct": [round(v * 100, 4) for v in boot["expectancy_ci"]],
        "expectancy_ci_bps": [round(v * 10_000, 2) for v in boot["expectancy_ci"]],
        "bootstrap": {"status": "ok", "method": "cluster_bootstrap_by_entry_month" if clusters is not None
                      else "iid_trade_bootstrap", "samples": samples, "seed": seed,
                      "confidence": confidence, "clusters": boot["clusters"]},
    })
    return result


def goal_assessment(stats: dict, *, min_trades: int) -> dict:
    """The owner's gate: >= 80% net win rate AND positive expectancy whose
    bootstrap CI lower bound is > 0, on enough trades. ``robust`` additionally
    needs the win-rate CI lower bound >= 80% and profit factor >= 1.3."""
    n = stats.get("trades") or 0
    win_rate = stats.get("win_rate")
    expectancy = stats.get("expectancy_bps")
    ci = stats.get("expectancy_ci_bps")
    win_ci = stats.get("win_rate_ci")
    checks = {
        "enough_trades": n >= min_trades,
        "win_rate_ge_80": win_rate is not None and win_rate >= GOAL_WIN_RATE,
        "positive_expectancy": expectancy is not None and expectancy > 0,
        "expectancy_ci_low_gt_0": ci is not None and ci[0] > 0,
    }
    meets = all(checks.values())
    robust = meets and win_ci is not None and win_ci[0] >= GOAL_WIN_RATE and (
        stats.get("profit_factor") is None or stats["profit_factor"] >= GOAL_PROFIT_FACTOR)
    return {"meets_goal": meets, "robust": bool(robust), "checks": checks, "min_trades": min_trades}


# --------------------------------------------------------------------------
# Variants
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class StrategyVariant:
    variant_id: str
    strategy: str
    parameters: dict = field(default_factory=dict)
    config_overrides: dict = field(default_factory=dict)
    family: str = ""
    # Optional restrictions (expensive strategies): only these symbols, data
    # from ``data_start``, and only these windows.
    symbols: Optional[tuple[str, ...]] = None
    data_start: Optional[str] = None
    windows: Optional[tuple[str, ...]] = None

    def config(self, base: Optional[BacktestConfig] = None) -> BacktestConfig:
        values = asdict(base or BacktestConfig())
        values.update(self.config_overrides)
        return BacktestConfig(**values)


EXISTING_STRATEGIES = (
    "moving_average_crossover", "macd_momentum", "rsi_mean_reversion", "bollinger_mean_reversion",
    "channel_breakout", "trend_pullback", "ensemble",
)

# Short-horizon reversion exits: indicator exit + time stop + 10% catastrophe
# stop, no profit target (take_profit far out of reach).
REVERSION_STOP = 0.10
NO_TARGET = 10.0


def default_variants(*, include_model_predictive: bool = False,
                     model_symbols: Sequence[str] = ("SPY", "QQQ", "AAPL", "MSFT"),
                     model_data_start: str = "2018-01-01") -> list[StrategyVariant]:
    variants = [StrategyVariant(slug, slug, family=slug) for slug in EXISTING_STRATEGIES]
    for threshold in (5, 10, 15):
        for exit_rule in ("sma", "up_close"):
            for time_stop in (5, 10):
                variants.append(StrategyVariant(
                    f"rsi2_pullback[rsi<{threshold},exit={exit_rule},t={time_stop}]",
                    "rsi2_pullback",
                    {"entry_threshold": threshold, "exit_rule": exit_rule},
                    {"stop_loss_pct": REVERSION_STOP, "take_profit_pct": NO_TARGET, "max_holding_days": time_stop},
                    family="rsi2_pullback",
                ))
    for threshold in (0.1, 0.2):
        for exit_rule in ("ibs", "up_close"):
            for time_stop in (5, 10):
                variants.append(StrategyVariant(
                    f"ibs_mean_reversion[ibs<{threshold},exit={exit_rule},t={time_stop}]",
                    "ibs_mean_reversion",
                    {"entry_threshold": threshold, "exit_rule": exit_rule},
                    {"stop_loss_pct": REVERSION_STOP, "take_profit_pct": NO_TARGET, "max_holding_days": time_stop},
                    family="ibs_mean_reversion",
                ))
    if include_model_predictive:
        # Causal (it refits on the completed-bar prefix at every bar) but costs
        # ~0.5 s per bar, so it is scored on a subset in the post-2020 window.
        variants.append(StrategyVariant(
            "model_predictive_long", "model_predictive_long", family="model_predictive_long",
            symbols=tuple(model_symbols), data_start=model_data_start, windows=("post2020",),
        ))
    return variants


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def normalize_prices(frame: pd.DataFrame, *, start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
    """Sorted daily OHLCV with ISO date strings; ``end`` is exclusive."""
    data = frame.copy()
    data.columns = [str(column).lower() for column in data.columns]
    data["date"] = pd.to_datetime(data["date"]).dt.strftime("%Y-%m-%d")
    data = data.dropna(subset=["open", "high", "low", "close"])
    data = data[(data["close"] > 0) & (data["open"] > 0)]
    if start:
        data = data[data["date"] >= start]
    if end:
        data = data[data["date"] < end]
    data = data.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    return data[["date", "open", "high", "low", "close", "volume"]]


def download_daily_history(symbol: str, *, start: str = "1990-01-01") -> pd.DataFrame:
    """Free public split/dividend-adjusted daily OHLCV from yfinance.

    Adjusted prices are required for a multi-decade backtest: raw prices show
    splits as -50%..-90% gaps that would trigger stops.
    """
    import yfinance as yf  # research-only dependency

    frame = yf.download(symbol, start=start, auto_adjust=True, progress=False, threads=False)
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    frame = frame.reset_index()
    frame.columns = [str(column).lower() for column in frame.columns]
    return normalize_prices(frame)


def load_daily_history(symbol: str, data_dir: Path, *, download: bool = False,
                       end: Optional[str] = None) -> pd.DataFrame:
    """Read ``<data_dir>/<symbol>.csv``; download and cache it when asked.

    ``end`` (exclusive, default today) drops a possibly incomplete current
    session bar.
    """
    data_dir = Path(data_dir)
    path = data_dir / f"{symbol}.csv"
    if download or not path.exists():
        if not download:
            raise FileNotFoundError(f"No cached history for {symbol} at {path}; pass download=True.")
        data_dir.mkdir(parents=True, exist_ok=True)
        download_daily_history(symbol).to_csv(path, index=False)
    return normalize_prices(pd.read_csv(path), end=end or date.today().isoformat())


# --------------------------------------------------------------------------
# One (variant, symbol) cell
# --------------------------------------------------------------------------

def _exposure_series(trades: list[dict], dates: np.ndarray, equity: np.ndarray,
                     closes: np.ndarray) -> np.ndarray:
    """Reconstruct the backtester's end-of-bar exposure (position value /
    equity) from the trade ledger. A position is marked on bars with
    entry_date <= date < exit_date."""
    held = np.zeros(len(dates))
    for trade in trades:
        lo = np.searchsorted(dates, trade["entry_date"], side="left")
        hi = np.searchsorted(dates, trade["exit_date"], side="left")
        if hi > lo:
            held[lo:hi] += float(trade["quantity"]) * closes[lo:hi]
    with np.errstate(divide="ignore", invalid="ignore"):
        exposure = np.where(equity > 0, held / equity, 0.0)
    return np.clip(exposure, 0.0, 1.0)


def _drawdown(values: np.ndarray) -> float:
    if values.size == 0:
        return 0.0
    peak = np.maximum.accumulate(values)
    return float(abs((values / peak - 1).min()))


def window_metrics(result: dict, prices: pd.DataFrame, window_start: Optional[str],
                   config: BacktestConfig, *, samples: int = 2000, seed: int = 7) -> Optional[dict]:
    """Trade and equity metrics for trades entered on/after ``window_start``.

    Returns None when the window has no evaluated bars (history starts later).
    """
    curve = result["equity_curve"]
    if not curve:
        return None
    curve_dates = np.asarray([row["date"] for row in curve])
    curve_values = np.asarray([row["value"] for row in curve], dtype=float)
    first_evaluated = curve_dates[0]
    start = max(window_start or first_evaluated, first_evaluated)
    position = int(np.searchsorted(curve_dates, start, side="left"))
    if position >= len(curve_dates):
        return None
    anchor = float(config.starting_cash) if position == 0 else float(curve_values[position - 1])
    values = np.concatenate([[anchor], curve_values[position:]])
    daily = values[1:] / values[:-1] - 1
    trades = [trade for trade in result["trades"] if trade["entry_date"] >= start]
    trade_returns = [float(trade["profit_loss_pct"]) for trade in trades]
    months = [trade["entry_date"][:7] for trade in trades]
    stats = trade_statistics(trade_returns, clusters=months, samples=samples, seed=seed)

    price_dates = prices["date"].to_numpy()
    closes = prices["close"].to_numpy(dtype=float)
    offset = FIRST_EVALUATED_INDEX
    exposure = _exposure_series(result["trades"], price_dates[offset:], curve_values, closes[offset:])
    window_exposure = float(exposure[position:].mean()) if exposure[position:].size else 0.0
    bh_start = int(np.searchsorted(price_dates, curve_dates[position], side="left"))
    bh_values = _buy_and_hold_values(prices, bh_start, config)
    bh_return = (bh_values[-1] / bh_values[0] - 1) if bh_values else None
    total_return = float(values[-1] / values[0] - 1)
    matched = None if bh_return is None else window_exposure * bh_return
    years = max(len(daily), 1) / 252
    moments = return_moments(daily)
    holding = [
        int(np.searchsorted(price_dates, trade["exit_date"]) - np.searchsorted(price_dates, trade["entry_date"]))
        for trade in trades
    ]
    exit_reasons: dict[str, int] = {}
    for trade in trades:
        exit_reasons[trade["exit_reason"]] = exit_reasons.get(trade["exit_reason"], 0) + 1
    return {
        "window_start": str(curve_dates[position]),
        "window_end": str(curve_dates[-1]),
        "bars": int(len(daily)),
        **stats,
        "trades_per_year": round(stats["trades"] / years, 2),
        "avg_holding_bars": round(float(np.mean(holding)), 2) if holding else None,
        "exit_reasons": exit_reasons,
        "total_return": round(total_return, 4),
        "max_drawdown": round(_drawdown(values), 4),
        "average_exposure": round(window_exposure, 4),
        "buy_and_hold_return": None if bh_return is None else round(bh_return, 4),
        "exposure_matched_buy_and_hold_return": None if matched is None else round(matched, 4),
        "excess_vs_buy_and_hold": None if bh_return is None else round(total_return - bh_return, 4),
        "excess_vs_exposure_matched": None if matched is None else round(total_return - matched, 4),
        "sharpe_per_period": round(moments["sharpe_per_period"], 6),
        "sharpe_annualized": round(moments["sharpe_per_period"] * math.sqrt(252), 4),
        "skewness": round(moments["skewness"], 4),
        "kurtosis": round(moments["kurtosis"], 4),
        "observations": moments["observations"],
        "_daily_dates": curve_dates[position:].tolist(),
        "_daily_returns": daily.tolist(),
        "_trade_returns": trade_returns,
        "_trade_months": months,
    }


def score_cell(symbol: str, variant: StrategyVariant, prices: pd.DataFrame,
               windows: dict[str, Optional[str]], *, base_config: Optional[BacktestConfig] = None,
               samples: int = 2000, seed: int = 7) -> dict:
    """Backtest one (variant, symbol) pair once and slice it into windows."""
    config = variant.config(base_config)
    frame = prices if not variant.data_start else prices[prices["date"] >= variant.data_start].reset_index(drop=True)
    cell = {"symbol": symbol, "variant_id": variant.variant_id, "strategy": variant.strategy,
            "family": variant.family, "parameters": variant.parameters,
            "config_overrides": variant.config_overrides, "windows": {}}
    if len(frame) <= FIRST_EVALUATED_INDEX + 1:
        cell["error"] = "insufficient history"
        return cell
    try:
        result = run_backtest(symbol, variant.strategy, frame, config, variant.parameters)
    except Exception as exc:  # pragma: no cover - surfaced in the report
        cell["error"] = f"{type(exc).__name__}: {exc}"
        return cell
    for name, start in windows.items():
        if variant.windows is not None and name not in variant.windows:
            continue
        metrics = window_metrics(result, frame, start, config, samples=samples, seed=seed)
        if metrics is not None:
            cell["windows"][name] = metrics
    return cell


def _score_cell_task(args: tuple) -> dict:
    return score_cell(*args[:4], samples=args[4], seed=args[5])


# --------------------------------------------------------------------------
# Pooling, deflation and ranking
# --------------------------------------------------------------------------

def _portfolio_returns(cells: list[dict], window: str) -> pd.Series:
    """Equal-weight daily return across the symbols live on each date."""
    series = []
    for cell in cells:
        metrics = cell["windows"].get(window)
        if metrics and metrics["_daily_returns"]:
            series.append(pd.Series(metrics["_daily_returns"], index=metrics["_daily_dates"]))
    if not series:
        return pd.Series(dtype=float)
    return pd.concat(series, axis=1).mean(axis=1, skipna=True).sort_index()


def pool_variant(cells: list[dict], window: str, *, samples: int = 2000, seed: int = 7) -> Optional[dict]:
    live = [cell for cell in cells if window in cell["windows"]]
    if not live:
        return None
    returns, months = [], []
    for cell in live:
        returns.extend(cell["windows"][window]["_trade_returns"])
        months.extend(cell["windows"][window]["_trade_months"])
    stats = trade_statistics(returns, clusters=months, samples=samples, seed=seed)
    portfolio = _portfolio_returns(live, window)
    moments = return_moments(portfolio.to_numpy())
    equity = np.concatenate([[1.0], np.cumprod(1 + portfolio.to_numpy())]) if len(portfolio) else np.array([1.0])
    metrics = [cell["windows"][window] for cell in live]

    def mean_of(key: str) -> Optional[float]:
        values = [m[key] for m in metrics if m.get(key) is not None]
        return round(float(np.mean(values)), 4) if values else None

    beats = [m["excess_vs_exposure_matched"] > 0 for m in metrics if m.get("excess_vs_exposure_matched") is not None]
    years = len(portfolio) / 252 if len(portfolio) else 0
    return {
        "symbols": len(live),
        **stats,
        "trades_per_year": round(stats["trades"] / years, 2) if years else None,
        "mean_symbol_total_return": mean_of("total_return"),
        "mean_buy_and_hold_return": mean_of("buy_and_hold_return"),
        "mean_exposure_matched_return": mean_of("exposure_matched_buy_and_hold_return"),
        "mean_excess_vs_buy_and_hold": mean_of("excess_vs_buy_and_hold"),
        "mean_excess_vs_exposure_matched": mean_of("excess_vs_exposure_matched"),
        "share_symbols_beating_exposure_matched": round(float(np.mean(beats)), 4) if beats else None,
        "mean_symbol_max_drawdown": mean_of("max_drawdown"),
        "portfolio_total_return": round(float(equity[-1] - 1), 4),
        "portfolio_max_drawdown": round(_drawdown(equity), 4),
        "average_exposure": mean_of("average_exposure"),
        "sharpe_per_period": round(moments["sharpe_per_period"], 6),
        "sharpe_annualized": round(moments["sharpe_per_period"] * math.sqrt(252), 4),
        "skewness": round(moments["skewness"], 4),
        "kurtosis": round(moments["kurtosis"], 4),
        "observations": moments["observations"],
    }


def _deflate(rows: list[dict], *, trials: int) -> None:
    sharpes = [row["sharpe_per_period"] for row in rows if row.get("observations", 0) > 1]
    for row in rows:
        dsr = deflated_sharpe_ratio(row["sharpe_per_period"], row.get("observations", 0), trials=trials,
                                    trial_sharpes=sharpes, skewness=row.get("skewness", 0.0),
                                    kurtosis=row.get("kurtosis", 3.0))
        row["deflated_sharpe"] = dsr["deflated_sharpe_ratio"]
        row["dsr_trials"] = trials
        row["dsr_expected_max_sharpe_per_period"] = dsr["expected_max_sharpe_per_period_under_null"]


def rank_key(row: dict) -> tuple:
    """Goal first, then the expectancy CI lower bound, then win rate."""
    ci = row.get("expectancy_ci_bps")
    return (
        bool(row.get("meets_goal")),
        bool(row.get("robust")),
        ci[0] if ci else -1e9,
        row.get("win_rate") or 0.0,
    )


def build_scorecard(cells: list[dict], variants: Sequence[StrategyVariant],
                    windows: dict[str, Optional[str]], *, samples: int = 2000, seed: int = 7) -> dict:
    by_variant: dict[str, list[dict]] = {}
    for cell in cells:
        by_variant.setdefault(cell["variant_id"], []).append(cell)
    variant_index = {variant.variant_id: variant for variant in variants}
    pooled: list[dict] = []
    per_symbol: list[dict] = []
    for window in windows:
        window_pooled, window_cells = [], []
        for variant_id, variant_cells in by_variant.items():
            row = pool_variant(variant_cells, window, samples=samples, seed=seed)
            if row is None:
                continue
            variant = variant_index[variant_id]
            row.update({"window": window, "variant_id": variant_id, "strategy": variant.strategy,
                        "family": variant.family})
            row.update(goal_assessment(row, min_trades=MIN_POOLED_TRADES))
            window_pooled.append(row)
            for cell in variant_cells:
                metrics = cell["windows"].get(window)
                if metrics is None:
                    continue
                public = {key: value for key, value in metrics.items() if not key.startswith("_")}
                public.update({"window": window, "variant_id": variant_id, "strategy": variant.strategy,
                               "family": variant.family, "symbol": cell["symbol"]})
                public.update(goal_assessment(public, min_trades=MIN_SYMBOL_TRADES))
                window_cells.append(public)
        # Every variant tried in the window is a trial; per-symbol picks also
        # select over symbols.
        _deflate(window_pooled, trials=len(window_pooled))
        _deflate(window_cells, trials=max(len(window_cells), 1))
        for row in window_pooled:
            winners = sorted(
                (c for c in window_cells if c["variant_id"] == row["variant_id"] and c["meets_goal"]),
                key=lambda c: -(c["expectancy_ci_bps"][0] if c.get("expectancy_ci_bps") else -1e9),
            )
            row["symbols_meeting_goal"] = [c["symbol"] for c in winners]
            high_win = [c for c in window_cells if c["variant_id"] == row["variant_id"]
                        and (c.get("win_rate") or 0) >= GOAL_WIN_RATE and (c.get("trades") or 0) >= MIN_SYMBOL_TRADES]
            row["symbols_win_rate_ge_80"] = sorted(c["symbol"] for c in high_win)
        pooled.extend(sorted(window_pooled, key=rank_key, reverse=True))
        per_symbol.extend(window_cells)
    errors = [{"symbol": c["symbol"], "variant_id": c["variant_id"], "error": c["error"]}
              for c in cells if c.get("error")]
    return {
        "goal": {"win_rate": GOAL_WIN_RATE, "min_pooled_trades": MIN_POOLED_TRADES,
                 "min_symbol_trades": MIN_SYMBOL_TRADES, "robust_profit_factor": GOAL_PROFIT_FACTOR},
        "windows": windows,
        "variants": [asdict(variant) for variant in variants],
        "trials_per_window": {w: sum(1 for row in pooled if row["window"] == w) for w in windows},
        "pooled": pooled,
        "per_symbol": per_symbol,
        "errors": errors,
    }


def run_scorecard(prices_by_symbol: dict[str, pd.DataFrame], variants: Sequence[StrategyVariant], *,
                  windows: Optional[dict[str, Optional[str]]] = None, workers: int = 1,
                  samples: int = 2000, seed: int = 7, progress=None) -> dict:
    windows = dict(windows or DEFAULT_WINDOWS)
    tasks = []
    for variant in variants:
        for symbol, prices in prices_by_symbol.items():
            if variant.symbols is not None and symbol not in variant.symbols:
                continue
            tasks.append((symbol, variant, prices, windows, samples, seed))
    cells: list[dict] = []
    if workers <= 1:
        for index, task in enumerate(tasks, 1):
            cells.append(_score_cell_task(task))
            if progress:
                progress(index, len(tasks), cells[-1])
    else:
        # Expensive tasks first so the pool does not idle on a long tail.
        order = sorted(range(len(tasks)), key=lambda i: tasks[i][1].strategy != "model_predictive_long")
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for index, cell in enumerate(pool.map(_score_cell_task, [tasks[i] for i in order], chunksize=1), 1):
                cells.append(cell)
                if progress:
                    progress(index, len(tasks), cell)
    return build_scorecard(cells, variants, windows, samples=samples, seed=seed)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

POOLED_COLUMNS = [
    "window", "variant_id", "family", "meets_goal", "robust", "symbols", "trades", "trades_per_year",
    "win_rate", "win_rate_wilson_ci", "win_rate_ci", "avg_win_pct", "avg_loss_pct", "payoff_ratio",
    "breakeven_win_rate", "expectancy_pct", "expectancy_bps", "expectancy_ci_bps", "profit_factor",
    "mean_symbol_total_return", "mean_buy_and_hold_return", "mean_exposure_matched_return",
    "mean_excess_vs_buy_and_hold", "mean_excess_vs_exposure_matched", "share_symbols_beating_exposure_matched",
    "mean_symbol_max_drawdown", "portfolio_total_return", "portfolio_max_drawdown", "average_exposure",
    "sharpe_annualized", "deflated_sharpe", "dsr_trials", "symbols_meeting_goal", "symbols_win_rate_ge_80",
]
CELL_COLUMNS = [
    "window", "variant_id", "family", "symbol", "meets_goal", "robust", "trades", "win_rate", "win_rate_wilson_ci",
    "win_rate_ci", "avg_win_pct", "avg_loss_pct", "payoff_ratio", "expectancy_pct", "expectancy_bps",
    "expectancy_ci_bps", "profit_factor", "total_return", "buy_and_hold_return",
    "exposure_matched_buy_and_hold_return", "excess_vs_buy_and_hold", "excess_vs_exposure_matched",
    "max_drawdown", "average_exposure", "avg_holding_bars", "sharpe_annualized", "deflated_sharpe", "dsr_trials",
    "window_start", "window_end",
]


def _fmt_ci(ci, scale: float = 1.0, digits: int = 1) -> str:
    return "n/a" if not ci else f"[{ci[0] * scale:.{digits}f}, {ci[1] * scale:.{digits}f}]"


def _pct(value, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def render_markdown(scorecard: dict, *, top: int = 12) -> str:
    lines = ["# Strategy scorecard (generated)", "",
             "Gate: net win rate >= 80% AND expectancy > 0 AND expectancy bootstrap 95% CI lower bound > 0 "
             f"AND >= {MIN_POOLED_TRADES} pooled trades ({MIN_SYMBOL_TRADES} per symbol). "
             "Robust = also win-rate CI lower bound >= 80% and profit factor >= 1.3.", ""]
    for window in scorecard["windows"]:
        rows = [row for row in scorecard["pooled"] if row["window"] == window]
        lines += [f"## Window: {window} (trials = {len(rows)})", "",
                  "| # | Variant | Goal | Trades | Win % [CI] | Avg win / loss % | Payoff | Exp. bps [CI] | PF | "
                  "Excess vs exp-matched B&H | DSR | Symbols meeting goal |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for rank, row in enumerate(rows[:top], 1):
            goal = "ROBUST" if row["robust"] else "yes" if row["meets_goal"] else "no"
            symbols = ", ".join(row["symbols_meeting_goal"][:8]) or "-"
            lines.append(
                f"| {rank} | {row['variant_id']} | {goal} | {row['trades']} | "
                f"{_pct(row['win_rate'])} {_fmt_ci(row['win_rate_ci'], 100)} | "
                f"{row['avg_win_pct']} / {row['avg_loss_pct']} | {row['payoff_ratio']} | "
                f"{row['expectancy_bps']} {_fmt_ci(row['expectancy_ci_bps'])} | {row['profit_factor']} | "
                f"{_pct(row['mean_excess_vs_exposure_matched'])} | {row['deflated_sharpe']} | {symbols} |"
            )
        lines.append("")
    if scorecard["errors"]:
        lines += ["## Errors", ""] + [f"- {e['variant_id']} / {e['symbol']}: {e['error']}" for e in scorecard["errors"]]
    return "\n".join(lines) + "\n"


def _csv_ready(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    frame = pd.DataFrame([{column: row.get(column) for column in columns} for row in rows], columns=columns)
    for column in columns:
        frame[column] = frame[column].map(lambda v: "|".join(map(str, v)) if isinstance(v, (list, tuple)) else v)
    return frame


def write_outputs(scorecard: dict, output_dir: Path) -> dict:
    import json

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "pooled_csv": output_dir / "scorecard_pooled.csv",
        "per_symbol_csv": output_dir / "scorecard_per_symbol.csv",
        "json": output_dir / "scorecard.json",
        "markdown": output_dir / "scorecard.md",
    }
    _csv_ready(scorecard["pooled"], POOLED_COLUMNS).to_csv(paths["pooled_csv"], index=False)
    _csv_ready(scorecard["per_symbol"], CELL_COLUMNS).to_csv(paths["per_symbol_csv"], index=False)
    paths["json"].write_text(json.dumps(scorecard, indent=1, default=str))
    paths["markdown"].write_text(render_markdown(scorecard))
    return {name: str(path) for name, path in paths.items()}
