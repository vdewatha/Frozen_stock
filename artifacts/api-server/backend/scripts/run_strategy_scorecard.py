"""Score every registered strategy (and the research-only RSI(2)/IBS candidates)
on daily history: win rate, payoff and expectancy net of costs, per symbol and
pooled, for the full history and the post-2015 / post-2020 windows.

Offline research only: reads cached yfinance CSVs (optionally downloads free
public daily history), runs the shared backtester, writes CSV/JSON/Markdown.
No database, broker or order code is touched.

Example (from the repository root):
  PYTHONPATH=artifacts/api-server/backend python \
    artifacts/api-server/backend/scripts/run_strategy_scorecard.py \
    --data-dir output/scorecard_data --output-dir output/strategy_scorecard --download --workers 8
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.strategy_scorecard import (  # noqa: E402
    DEFAULT_WINDOWS,
    default_variants,
    load_daily_history,
    run_scorecard,
    write_outputs,
)


def _default_symbols() -> list[str]:
    from app.core.config import settings

    return list(settings.paper_execution_symbols)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", help="Comma-separated symbols (default: settings.paper_execution_symbols)")
    parser.add_argument("--data-dir", type=Path, required=True, help="Cache of <SYMBOL>.csv daily OHLCV files")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--download", action="store_true", help="(Re)download history with yfinance into --data-dir")
    parser.add_argument("--end", help="Exclusive end date (default: today, drops a partial session)")
    parser.add_argument("--families", help="Comma-separated strategy families to run (default: all)")
    parser.add_argument("--include-model-predictive", action="store_true",
                        help="Also score model_predictive_long (slow: refits per bar) on --model-symbols, post-2020")
    parser.add_argument("--model-symbols", default="SPY,QQQ,AAPL,MSFT")
    parser.add_argument("--model-data-start", default="2018-01-01")
    parser.add_argument("--workers", type=int, default=max((os.cpu_count() or 2) - 1, 1))
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else _default_symbols()
    prices = {}
    for symbol in symbols:
        prices[symbol] = load_daily_history(symbol, args.data_dir, download=args.download, end=args.end)
        print(f"{symbol}: {len(prices[symbol])} bars {prices[symbol]['date'].iloc[0]} .. "
              f"{prices[symbol]['date'].iloc[-1]}", flush=True)
    variants = default_variants(
        include_model_predictive=args.include_model_predictive,
        model_symbols=[s.strip().upper() for s in args.model_symbols.split(",")],
        model_data_start=args.model_data_start,
    )
    if args.families:
        wanted = {f.strip() for f in args.families.split(",")}
        variants = [v for v in variants if v.family in wanted]
    started = time.time()

    def progress(index, total, cell):
        if index % 25 == 0 or index == total:
            print(f"[{index}/{total}] {time.time() - started:.0f}s {cell['variant_id']} {cell['symbol']}", flush=True)

    scorecard = run_scorecard(prices, variants, windows=DEFAULT_WINDOWS, workers=args.workers,
                              samples=args.bootstrap_samples, seed=args.seed, progress=progress)
    scorecard["data"] = {symbol: {"bars": len(frame), "first": frame["date"].iloc[0], "last": frame["date"].iloc[-1]}
                         for symbol, frame in prices.items()}
    scorecard["data_source"] = "yfinance daily, split/dividend adjusted (auto_adjust=True)"
    for name, path in write_outputs(scorecard, args.output_dir).items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
