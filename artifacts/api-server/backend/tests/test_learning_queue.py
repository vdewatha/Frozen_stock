"""Short learning jobs get their own lane; the nightly sweep cannot run into the session.

On 2026-11-06 the nightly fallback sweep (every active asset, ~2 minutes
each) was still running at 9:18 ET. It held one of the compact worker's two
threads, the 30-minute attribution and news jobs expired, and no closed trade
had been explained since 2026-11-03.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.core.config import settings
from app.tasks import jobs
from app.tasks.celery_app import LEARNING_FAST_QUEUE, celery_app

UTC = timezone.utc
SERVER = Path(__file__).resolve().parents[2] / "src" / "production-server.ts"


def _sweep(deadline_offset: timedelta, symbols=("SPY", "AAPL", "MSFT")):
    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = SimpleNamespace(
        strategy_type="moving_average_crossover")
    swept = []

    def experiments(_db, *, symbol, **_kwargs):
        swept.append(symbol)
        return {"symbol": symbol, "experiments": []}

    with patch.object(settings, "paper_execution_symbols", list(symbols)), \
            patch.object(jobs, "_run_job", lambda _name, work: work(db)), \
            patch.object(jobs, "run_strategy_experiments", experiments), \
            patch("app.services.parameter_application.run_loss_driven_experiments",
                  return_value={"agenda": {"items": []}, "runs": []}), \
            patch("app.services.intraday_data.next_regular_session_open",
                  return_value=datetime.now(UTC) + jobs.NIGHTLY_SWEEP_OPEN_MARGIN + deadline_offset):
        result = jobs.nightly_backtest_job.run()
    return result, swept


def test_nightly_sweep_covers_only_the_execution_universe():
    result, swept = _sweep(timedelta(hours=10))
    # Not every active asset (news-discovered and old watchlist names).
    assert sorted(swept) == ["AAPL", "MSFT", "SPY"]
    assert result["mode"] == "fixed_fallback" and result["stopped_before_open_at"] is None


def test_nightly_sweep_stops_starting_symbols_before_the_open():
    result, swept = _sweep(-timedelta(minutes=1))
    assert swept == [] and result["stopped_before_open_at"] is not None


def test_short_learning_jobs_route_to_the_fast_lane():
    routes = celery_app.conf.task_routes
    for task in ("app.tasks.jobs.trade_attribution_job", "app.tasks.jobs.news_discovery_job"):
        assert routes[task] == {"queue": LEARNING_FAST_QUEUE}


def _queues(hostname: str) -> set[str]:
    source = SERVER.read_text()
    match = re.search(rf'--hostname={re.escape(hostname)}@%h",\s*"--queues=([^"`]+)', source)
    assert match, hostname
    return set(match.group(1).split(","))


def test_every_worker_layout_serves_the_fast_lane():
    # Compact (hosted) layout: the fast lane serves it even when paper-core is busy.
    assert LEARNING_FAST_QUEUE in _queues("paper-fast")
    assert LEARNING_FAST_QUEUE in _queues("paper-core")
    # Full layout: the learning replicas.
    assert re.search(r'"--queues=learning,learning_fast"', SERVER.read_text())
    for compose in ("compose.yaml", "compose.oracle-micro.yaml"):
        text = (Path(__file__).resolve().parents[4] / "deploy" / "paper" / compose).read_text()
        assert '"--queues=learning,learning_fast"' in text, compose
