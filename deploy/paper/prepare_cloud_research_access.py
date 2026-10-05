"""Export only paper market-data credentials, never live or local role keys."""
import argparse
import os
from pathlib import Path

from dotenv import dotenv_values


def prepare(source: Path, destination: Path) -> None:
    config = dotenv_values(source, interpolate=False)
    names = {
        "PAPER_ALPACA_API_KEY": ("PAPER_ALPACA_API_KEY", "ALPACA_API_KEY"),
        "PAPER_ALPACA_API_SECRET": ("PAPER_ALPACA_API_SECRET", "ALPACA_API_SECRET"),
        "TRADIER_MARKET_DATA_API_KEY": ("TRADIER_MARKET_DATA_API_KEY",),
    }
    values = {target: next((config.get(key) for key in keys if config.get(key)), "")
              for target, keys in names.items()}
    if not values["PAPER_ALPACA_API_KEY"] or not values["PAPER_ALPACA_API_SECRET"]:
        raise ValueError("Paper Alpaca credentials are required")
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("Credentials must be single-line values")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        for key, value in values.items():
            handle.write(f"{key}='{value.replace(chr(39), chr(92) + chr(39))}'\n")
    print("Private research credentials exported; no live or role credentials copied.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source, args.output)
