"""Fixed credential-free observation configuration, not model approval."""
from __future__ import annotations

import json
import os
from pathlib import Path


def kraken_observation_config(username: str, password: str, jwt_secret: str) -> dict:
    if not username or ":" in username or any(c.isspace() for c in username):
        raise ValueError("A nonempty API username without whitespace or colon is required")
    if any(len(secret) < 32 or any(c.isspace() for c in secret) for secret in (password, jwt_secret)) or password == jwt_secret:
        raise ValueError("Distinct API password and JWT secrets of at least 32 characters are required")
    return {
        "dry_run": True, "trading_mode": "spot", "max_open_trades": 1,
        "stake_currency": "USD", "stake_amount": 100, "dry_run_wallet": 10000,
        "tradable_balance_ratio": 0.99, "timeframe": "1h",
        "strategy": "ObservationOnly", "initial_state": "stopped",
        "force_entry_enable": False, "cancel_open_orders_on_exit": True,
        "db_url": "sqlite:///trades-observation.dryrun.sqlite",
        "exchange": {"name": "kraken", "key": "", "secret": "",
                     "pair_whitelist": ["BTC/USD"], "pair_blacklist": [],
                     "ccxt_config": {"enableRateLimit": True},
                     "ccxt_async_config": {"enableRateLimit": True}},
        "pairlists": [{"method": "StaticPairList"}],
        "entry_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1},
        "exit_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1},
        "unfilledtimeout": {"entry": 10, "exit": 10, "unit": "minutes"},
        "telegram": {"enabled": False, "token": "", "chat_id": ""},
        "api_server": {"enabled": True, "listen_ip_address": "127.0.0.1", "listen_port": 8080,
                       "verbosity": "error", "enable_openapi": False, "CORS_origins": [],
                       "username": username, "password": password, "jwt_secret_key": jwt_secret},
        "bot_name": "kraken-paper-observation",
    }


def kraken_execution_config(username: str, password: str, jwt_secret: str) -> dict:
    """Explicit simulated-order profile. This is not capital/model approval."""
    config = kraken_observation_config(username, password, jwt_secret)
    config.update(bot_name="kraken-paper-execution", force_entry_enable=True,
                  initial_state="running", db_url="sqlite:///trades-execution.dryrun.sqlite")
    return config


def write_config(path: Path, config: dict) -> None:
    """Exclusive private file; never print credentials or overwrite a config."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(config, handle, indent=2, allow_nan=False)
        handle.write("\n")
