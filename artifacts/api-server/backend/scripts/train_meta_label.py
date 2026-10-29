"""Train (and optionally walk-forward evaluate) the meta-label BUY filter offline.

Produces the artifact the paper cycle loads in shadow mode:

    <backend>/stock_training_artifacts/meta_label/meta_label_model.joblib
    <backend>/stock_training_artifacts/meta_label/meta_label_model.joblib.json  (manifest + sha256)

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/train_meta_label.py --data-dir /tmp/ohlcv --download
    PYTHONPATH=. python scripts/train_meta_label.py --data-dir /tmp/ohlcv --walk-forward-json wf.json

``--data-dir`` holds one ``<SYMBOL>.csv`` per symbol (date, open, high, low,
close, volume; split/dividend-adjusted).  ``--download`` fills it from free
public yfinance daily history (no broker, no credentials).  Training is
deterministic for a fixed data snapshot (seeded models, ordered inputs).
Nothing here touches a broker, the database or any order path.
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS  # noqa: E402
from app.services import meta_label  # noqa: E402
from app.services.meta_label_shadow import model_path  # noqa: E402
from app.services.triple_barrier import BarrierConfig  # noqa: E402


def download(data_dir: Path, symbols: list[str], start: str, end: str | None) -> None:
    import yfinance as yf

    data_dir.mkdir(parents=True, exist_ok=True)
    for symbol in symbols:
        frame = yf.download(symbol, start=start, end=end, auto_adjust=True, progress=False, threads=False)
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        frame = frame.reset_index().rename(columns=str.lower)[["date", "open", "high", "low", "close", "volume"]]
        frame.to_csv(data_dir / f"{symbol}.csv", index=False)
        print(f"downloaded {symbol}: {len(frame)} bars", flush=True)


def load(data_dir: Path, symbols: list[str]) -> dict[str, pd.DataFrame]:
    prices = {}
    for symbol in symbols:
        path = data_dir / f"{symbol}.csv"
        if path.exists():
            prices[symbol] = pd.read_csv(path).sort_values("date").reset_index(drop=True)
        else:
            print(f"warning: no data for {symbol}", file=sys.stderr)
    if "SPY" not in prices:
        raise SystemExit("SPY history is required for regime features")
    return prices


def _clean(value):
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, np.integer):
        return int(value)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--start", default="2008-06-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--symbols", nargs="*", default=list(DEFAULT_PAPER_EXECUTION_SYMBOLS))
    parser.add_argument("--output", type=Path, default=None, help="artifact path (default: the runtime model path)")
    parser.add_argument("--model-kind", default="gradient_boosting", choices=["gradient_boosting", "logistic_regression"])
    parser.add_argument("--calibration", default="isotonic", choices=["isotonic", "platt"])
    parser.add_argument("--min-win-rate", type=float, default=0.80)
    parser.add_argument("--min-trades", type=int, default=30)
    parser.add_argument("--objective", default="expectancy", choices=["expectancy", "total"])
    parser.add_argument("--volatility-scaled", action="store_true", help="1-sigma*sqrt(H) barriers instead of fixed 3%%/3%%")
    parser.add_argument("--max-holding-days", type=int, default=10)
    parser.add_argument("--walk-forward-json", type=Path, default=None, help="also run the yearly walk-forward")
    parser.add_argument("--first-test-year", type=int, default=2014)
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args(argv)
    warnings.filterwarnings("ignore", category=RuntimeWarning)

    symbols = sorted({symbol.upper() for symbol in args.symbols} | {"SPY"})
    if args.download:
        download(args.data_dir, symbols, args.start, args.end)
    prices = load(args.data_dir, symbols)
    barrier = (BarrierConfig(volatility_scaled=True, max_holding_days=args.max_holding_days)
               if args.volatility_scaled else BarrierConfig(max_holding_days=args.max_holding_days))
    events = meta_label.build_events(prices, prices["SPY"], barrier)
    data_end = max(str(frame["date"].iloc[-1])[:10] for frame in prices.values())
    print(f"events: {len(events)} ({events['t0'].min().date()} .. {events['t0'].max().date()}), data end {data_end}")
    policy = meta_label.ThresholdPolicy(min_win_rate=args.min_win_rate, min_trades=args.min_trades,
                                        objective=args.objective)
    config = meta_label.WalkForwardConfig(first_test_year=args.first_test_year, model_kind=args.model_kind,
                                          calibration=args.calibration, policy=policy)

    if args.walk_forward_json:
        result = meta_label.walk_forward(events, config)
        predictions = result["predictions"]
        report = {"config": {"barrier": barrier.to_dict(), "model_kind": args.model_kind,
                             "calibration": args.calibration, "policy": policy.__dict__},
                  "audit": result["audit"], "periods": {}}
        for name, mask in (("all", np.ones(len(predictions), bool)),
                           ("post_2020", (predictions["t0"] >= "2020-01-01").to_numpy())):
            subset = predictions.loc[mask]
            period = {"take_all": meta_label.summarize(subset, np.ones(len(subset), bool)),
                      "filter": meta_label.summarize(subset), "targets": {}}
            for target in (0.55, 0.60, 0.65, 0.70, 0.75, 0.80):
                take, _ = meta_label.reselect(result, meta_label.ThresholdPolicy(
                    min_win_rate=target, min_trades=args.min_trades, objective=args.objective))
                period["targets"][str(target)] = meta_label.summarize(subset, take[mask])
            period["fixed_thresholds"] = meta_label.frontier(subset, [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9])
            report["periods"][name] = period
        args.walk_forward_json.parent.mkdir(parents=True, exist_ok=True)
        args.walk_forward_json.write_text(json.dumps(_clean(report), indent=2, default=str) + "\n")
        print(f"walk-forward report: {args.walk_forward_json}")

    bundle = meta_label.fit_final_model(events, config, barrier=barrier, data_end=data_end)
    bundle["data"] = {"symbols": sorted(prices), "start": args.start, "data_end": data_end, "events": int(len(events)),
                      "source": "yfinance daily, auto_adjust=True"}
    print("final threshold:", bundle["threshold"], json.dumps(_clean(bundle["selection"]), default=str))
    if not args.no_save:
        output = args.output or model_path()
        manifest = meta_label.save_model(bundle, output)
        print(f"saved {output} sha256={manifest['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
