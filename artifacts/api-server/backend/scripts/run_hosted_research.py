"""Dedicated hosted multi-symbol research worker; never an execution worker."""

from __future__ import annotations

import logging
import os
import threading

from app.core.config import settings
from app.services.iex_research_runner import run_iex_research_loop
from app.services.scalp_research_runner import run_scalp_research_loop


def main() -> None:
    if settings.environment != "production":
        raise SystemExit("hosted research worker requires ENVIRONMENT=production")
    if settings.allow_live_trading:
        raise SystemExit("hosted research worker refuses live-enabled configuration")
    if os.environ.get("PAPER_HOSTED_RESEARCH_WORKER", "false").lower() != "true":
        raise SystemExit("set PAPER_HOSTED_RESEARCH_WORKER=true to run the research worker")
    symbols = sorted({
        value.strip().upper()
        for value in os.environ.get("PAPER_SCALP_RESEARCH_SYMBOLS", "AAPL,MSFT,QQQ,SPY").split(",")
        if value.strip()
    })
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop_event = threading.Event()
    threads = [threading.Thread(target=run_iex_research_loop, kwargs={"stop_event": stop_event}, daemon=True)]
    threads.extend(
        threading.Thread(
            target=run_scalp_research_loop,
            args=(symbol,),
            kwargs={"stop_event": stop_event},
            name=f"scalp-research-{symbol.lower()}",
            daemon=True,
        )
        for symbol in symbols
    )
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
