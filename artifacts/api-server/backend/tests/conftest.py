"""Suite-wide test fixtures."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.db import schema  # Register the complete FK graph for isolated DB fixtures.
from app.db.base import Base

POSTGRES_SKIP_REASON = "Requires PostgreSQL: set DATABASE_URL to an isolated test database"


def _database_is_postgres() -> bool:
    return make_url(settings.database_url).get_backend_name() == "postgresql"


def pytest_addoption(parser):
    parser.addoption(
        "--require-postgres", action="store_true", default=False,
        help="Fail (instead of skip) every test marked 'postgres'; the PostgreSQL CI job runs with this.",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "postgres: needs a real PostgreSQL server (row locks, advisory locks, ON CONFLICT). "
        "Skipped on SQLite; a skip is a failure under --require-postgres.",
    )
    if config.getoption("--require-postgres") and not _database_is_postgres():
        raise pytest.UsageError("--require-postgres needs DATABASE_URL to point at PostgreSQL")


def pytest_collection_modifyitems(config, items):
    if _database_is_postgres():
        return
    skip = pytest.mark.skip(reason=POSTGRES_SKIP_REASON)
    for item in items:
        if item.get_closest_marker("postgres"):
            item.add_marker(skip)


def pytest_collection_finish(session):
    # An empty selection would pass with nothing run (a renamed marker, a bad -m).
    if session.config.getoption("--require-postgres") and not any(
        item.get_closest_marker("postgres") for item in session.items
    ):
        raise pytest.UsageError("--require-postgres selected no test marked 'postgres'")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if (
        report.skipped
        # A strict expected failure ran and failed as recorded; it is not a skip.
        and not hasattr(report, "wasxfail")
        and item.config.getoption("--require-postgres")
        and item.get_closest_marker("postgres")
    ):
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        report.outcome = "failed"
        report.longrepr = (
            f"{item.nodeid} is marked 'postgres' and was skipped under --require-postgres: {reason}"
        )


@pytest.fixture()
def postgres_schema():
    """A throwaway PostgreSQL schema holding every table; dropped after the test.

    Connections from ``engine`` see only that schema (``search_path``), so a
    test never touches the database's own tables. ``owner`` connects without
    the search path, for ``pg_blocking_pids`` and other server-wide checks.
    Advisory locks are server-wide and are not isolated by the schema.
    """
    url = make_url(settings.database_url)
    if url.get_backend_name() != "postgresql":
        pytest.skip(POSTGRES_SKIP_REASON)
    owner = create_engine(url)
    name = "pgtest_" + uuid4().hex
    with owner.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_engine(
        # A lock wait this long is a hang; fail with 55P03 instead of stalling the job.
        url.update_query_dict({"options": f"-csearch_path={name} -clock_timeout=60s"}),
        pool_size=10, max_overflow=10,
    )
    try:
        Base.metadata.create_all(engine)
        yield SimpleNamespace(engine=engine, owner=owner, name=name)
    finally:
        engine.dispose()
        with owner.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        owner.dispose()


@pytest.fixture(autouse=True)
def _wall_clock_independent_entry_window(monkeypatch):
    """Keep cycle tests independent of the real wall clock.

    The time-of-day entry protection reads the real exchange calendar, so a
    cycle test that runs during the first/last minutes of a real session would
    otherwise see its BUYs skipped.  Tests that exercise the filter set these
    explicitly (risk-rule keys or ``patch.object``) and inject the clock.
    """
    monkeypatch.setattr(settings, "paper_entry_open_buffer_minutes", 0)
    monkeypatch.setattr(settings, "paper_entry_close_buffer_minutes", 0)
    # Monitor pause tuning also reads the real clock; tests that exercise it
    # enable it explicitly with an injected clock.
    monkeypatch.setattr(settings, "paper_monitor_offhours_deferral_enabled", False)
    monkeypatch.setattr(settings, "paper_monitor_open_feed_grace_minutes", 0)


@pytest.fixture(autouse=True)
def _no_network_earnings_calendar(monkeypatch):
    """The earnings blackout fetches from yfinance; tests never hit the network.

    Tests that exercise the blackout set the window and inject a fetch.
    """
    monkeypatch.setattr(settings, "paper_earnings_blackout_sessions", 0)
    monkeypatch.setattr(settings, "loss_diagnosis_earnings_lookup", False)
    # News discovery reads Alpaca news and the database; its tests enable it.
    monkeypatch.setattr(settings, "news_discovery_enabled", False)
    # The announcement blackout would make cycle tests depend on today's date
    # (a run the session before an FOMC meeting); its tests enable it.
    monkeypatch.setattr(settings, "market_event_blackout_enabled", False)
    # The point-in-time replay universe downloads the S&P 500 membership file
    # and every past member's history; its tests enable it with injected
    # membership and loaders.
    monkeypatch.setattr(settings, "replay_point_in_time_universe", False)
    from app.services import market_events
    market_events.clear_cache()


@pytest.fixture()
def legacy_technical_exits(monkeypatch):
    """Give the technical strategies the pre-2026-11-05 "no declared exit plan" behaviour.

    They now trade their backtested exits (no SELL-signal exits).  Tests of
    the generic SELL path were written against them as strategies with no
    exit plan; this keeps those tests about the SELL path.
    """
    from app.services import stock_paper_autotrader
    from app.services.strategies import technical

    original = stock_paper_autotrader.exit_policy_for
    slugs = {cls.slug for cls in vars(technical).values()
             if isinstance(cls, type) and getattr(cls, "default_exit_policy", None)
             is technical.BACKTESTED_BRACKET_EXIT_POLICY}

    def exit_policy_for(row):
        return None if getattr(row, "strategy_type", None) in slugs else original(row)

    monkeypatch.setattr(stock_paper_autotrader, "exit_policy_for", exit_policy_for)


@pytest.fixture(autouse=True)
def _no_vix_download():
    """Seeded lineups carry a ``vix_gate``; never reach yfinance from a test.

    An empty pinned history makes the gate fail open (BUYs pass, flagged
    ``vix_unavailable``), so tests that are not about the gate are unchanged.
    """
    from app.services.strategies.vix_gate import set_vix_history

    set_vix_history({})
    yield
    set_vix_history(None)
