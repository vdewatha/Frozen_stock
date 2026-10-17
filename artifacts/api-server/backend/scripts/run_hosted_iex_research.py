"""Dedicated hosted IEX research worker; never an execution worker."""

from __future__ import annotations

import logging
import os

from app.core.config import settings
from app.services.iex_research_runner import run_iex_research_loop


def main() -> None:
    if settings.environment != "production":
        raise SystemExit("hosted IEX worker requires ENVIRONMENT=production")
    if settings.allow_live_trading:
        raise SystemExit("hosted IEX worker refuses live-enabled configuration")
    if os.environ.get("PAPER_HOSTED_RESEARCH_WORKER", "false").lower() != "true":
        raise SystemExit("set PAPER_HOSTED_RESEARCH_WORKER=true to run the research worker")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_iex_research_loop()


if __name__ == "__main__":
    main()
