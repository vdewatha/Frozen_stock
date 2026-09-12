"""Run deployment hardening checks and emit a safe JSON report."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db.session import SessionLocal
from app.services.operational_hardening import run_operational_hardening


def main() -> int:
    db = SessionLocal()
    try:
        result = run_operational_hardening(db, source="deployment_probe")
        print(json.dumps(result, default=str, sort_keys=True))
        return 1 if result["status"] == "breach" else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())