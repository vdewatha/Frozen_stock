#!/usr/bin/env python3
"""Read-only dry-run check. Set FREQTRADE_URL, FREQTRADE_USERNAME and
FREQTRADE_PASSWORD in the environment. No secrets are accepted as CLI arguments.
Run: python backend/scripts/check_freqtrade.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.integrations.freqtrade import FreqtradeClient, FreqtradeError


def main() -> int:
    try:
        timeout = float(os.environ.get("FREQTRADE_TIMEOUT_SECONDS", "5"))
    except ValueError:
        print(json.dumps({"ok": False, "error": "Invalid FREQTRADE_TIMEOUT_SECONDS."}))
        return 1
    try:
        with FreqtradeClient(
            os.environ.get("FREQTRADE_URL", "http://127.0.0.1:8080"),
            os.environ.get("FREQTRADE_USERNAME", ""),
            os.environ.get("FREQTRADE_PASSWORD", ""), timeout=timeout,
        ) as client:
            client.ping()
            result = client.status()
        print(json.dumps({"ok": True, **result}))
        return 0
    except FreqtradeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
