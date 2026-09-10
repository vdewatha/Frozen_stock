"""Run from repository root: python backend/scripts/train_research.py --help."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from app.services.research_training import train_research_run


def main():
    parser = argparse.ArgumentParser(description="Train offline experimental models; never authorizes trading")
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--source", required=True, help="Research provenance claim, not certified provider identity")
    parser.add_argument("--horizon", type=int, choices=[1, 5, 20], default=5)
    parser.add_argument("--instrument-id", choices=["crypto_spot:KRAKEN:BTC:USD"])
    parser.add_argument("--timeframe-minutes", type=int, choices=[60])
    args = parser.parse_args()
    result = train_research_run(pd.read_csv(args.csv), args.output, symbol=args.symbol, source=args.source, horizon=args.horizon,
                                instrument_id=args.instrument_id, timeframe_minutes=args.timeframe_minutes)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
