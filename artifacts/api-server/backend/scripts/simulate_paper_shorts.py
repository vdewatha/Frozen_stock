"""Run isolated short research against existing daily data without broker I/O."""
import argparse
import json

from app.db.session import SessionLocal
from app.services.market_data import get_price_history
from app.services.paper_short_simulation import ShortSimulationConfig, simulate_short_history


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol")
    parser.add_argument("--source", choices=["alpaca_iex_daily", "yfinance", "yahoo_chart"], default="alpaca_iex_daily")
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    if not 202 <= args.limit <= 5000:
        parser.error("limit must be between 202 and 5000")
    with SessionLocal() as db:
        frame, _ = get_price_history(db, args.symbol.upper(), limit=args.limit,
                                     auto_seed=False, source_filter=args.source)
        try:
            report = simulate_short_history(frame, symbol=args.symbol.upper(), source=args.source,
                                             config=ShortSimulationConfig())
        except ValueError as exc:
            parser.exit(2, f"Short simulation blocked: {exc}\n")
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
