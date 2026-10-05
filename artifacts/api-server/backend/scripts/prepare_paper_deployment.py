"""Generate private local paper-stack credentials without overwriting a deployment."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets


def prepare(path: Path) -> None:
    values = {
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        **{f"AUTH_{role}_KEY": secrets.token_hex(32) for role in (
            "VIEWER", "RESEARCHER", "OPERATOR", "ADMIN",
        )},
        "PAPER_API_PORT": "8010",
        "LEARNING_CONCURRENCY": "1",
        "STOCK_LEARNING_DEFAULT_SYMBOLS": '["AAPL","MSFT","QQQ","SPY"]',
        "STOCK_LEARNING_DEFAULT_PROVIDER": "yfinance",
        "ALLOW_LIVE_TRADING": "false",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("# Private paper-only configuration. Broker credentials are intentionally absent.\n")
        for name, value in values.items():
            handle.write(f"{name}={value}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.output)
    except FileExistsError:
        parser.exit(1, "Configuration already exists; no credentials were changed.\n")
    print("Private paper configuration created; broker access and trading approval remain unconfigured.")
