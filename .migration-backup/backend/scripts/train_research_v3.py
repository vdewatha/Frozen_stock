"""Train an offline calibrated candidate from a verified year-long history bundle."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.kraken_history import load_history_bundle
from app.services.research_training_v3 import train_research_v3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history-bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fee-rate', type=float, default=.008)
    parser.add_argument('--slippage-rate', type=float, default=.001)
    args = parser.parse_args()
    prices, history = load_history_bundle(args.history_bundle)
    if len(prices) < 8760:
        parser.error('Requires at least 8760 contiguous hourly observations')
    result = train_research_v3(prices, args.output,
        source='kraken-history-bundle:' + history['snapshot_id'],
        fee_rate=args.fee_rate, slippage_rate=args.slippage_rate)
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
