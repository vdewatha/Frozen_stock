"""Suite-wide test fixtures."""
import pytest

from app.core.config import settings
from app.db import schema  # Register the complete FK graph for isolated DB fixtures.


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


@pytest.fixture(autouse=True)
def _no_network_earnings_calendar(monkeypatch):
    """The earnings blackout fetches from yfinance; tests never hit the network.

    Tests that exercise the blackout set the window and inject a fetch.
    """
    monkeypatch.setattr(settings, "paper_earnings_blackout_sessions", 0)
    monkeypatch.setattr(settings, "loss_diagnosis_earnings_lookup", False)
    # News discovery reads Alpaca news and the database; its tests enable it.
    monkeypatch.setattr(settings, "news_discovery_enabled", False)


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
