"""Import a caller-downloaded Kraken CSV into an immutable offline research bundle."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.kraken_history import import_kraken_history, SOURCE_URL


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-url", required=True, choices=[SOURCE_URL])
    parser.add_argument("--minimum-days", type=int, default=365)
    parser.add_argument("--start", help="Explicit UTC-aware inclusive hour, e.g. 2023-01-01T00:00:00+00:00")
    parser.add_argument("--end", help="Explicit UTC-aware exclusive hour, e.g. 2026-01-01T00:00:00+00:00")
    args = parser.parse_args()
    try:
        result = import_kraken_history(args.csv, args.output, source_url=args.source_url,
                                      minimum_days=args.minimum_days, start=args.start, end=args.end)
    except (ValueError, OSError):
        parser.error("History import rejected; verify schema, identity, continuity, source and new output location")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
