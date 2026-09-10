"""Explicit offline v2 research; never changes runtime bindings."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from app.services.research_training_v2 import train_research_v2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--csv", type=Path)
    inputs.add_argument("--history-bundle", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source")
    parser.add_argument("--horizon", type=int, default=24)
    parser.add_argument("--fee-rate", type=float, default=.01)
    parser.add_argument("--slippage-rate", type=float, default=.001)
    args = parser.parse_args()
    if args.history_bundle:
        from app.services.kraken_history import load_history_bundle
        prices, history = load_history_bundle(args.history_bundle)
        source = "kraken-history-bundle:" + history["snapshot_id"]
    else:
        if not args.source: parser.error("CSV input requires explicit --source")
        prices = pd.read_csv(args.csv)
        source = args.source
    if len(prices) < 8760:
        parser.error("V2 research CLI requires at least 8760 contiguous hourly observations")
    result = train_research_v2(prices, args.output, source=source, horizon=args.horizon,
                               fee_rate=args.fee_rate, slippage_rate=args.slippage_rate)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
