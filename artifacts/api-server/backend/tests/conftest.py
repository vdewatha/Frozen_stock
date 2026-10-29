"""Suite-wide test fixtures."""
import pytest

from app.core.config import settings


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
