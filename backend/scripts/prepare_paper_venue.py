"""Create observation-only Freqtrade config. No exchange account credentials."""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.integrations.paper_venue import kraken_observation_config, kraken_execution_config, write_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--paper-execution", action="store_true", help="Explicitly allow simulated API orders; never live orders")
    args = parser.parse_args()
    try:
        factory = kraken_execution_config if args.paper_execution else kraken_observation_config
        config = factory(os.environ.get("FREQTRADE_USERNAME", ""),
            os.environ.get("FREQTRADE_PASSWORD", ""), os.environ.get("FREQTRADE_JWT_SECRET", ""))
        write_config(args.output, config)
    except (ValueError, OSError):
        print("Configuration not written. Check distinct secrets and a new writable output path.", file=sys.stderr)
        return 1
    print("Paper-only configuration written. No service started; no model approved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
