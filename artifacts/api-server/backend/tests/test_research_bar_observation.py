from datetime import timedelta

import pytest

from app.models import IntradayBar, OnlineResearchForecast
from app.services.intraday_data import _aware_utc, upsert_intraday_bars
from tests.test_online_research import db, START, seed, run


def ingest(db, minute, *, observed, close=101, provider="alpaca_iex", feed="iex"):
    upsert_intraday_bars(db, "SPY", [{"t": (START+timedelta(minutes=minute)).isoformat(),
        "o": close, "h": close, "l": close, "c": close, "v": 100}],
        ingested_at=START+timedelta(minutes=observed), provider=provider, feed_class=feed)


@pytest.mark.parametrize("provider,feed,first", [
    ("alpaca_iex", "iex", 2), ("alpaca_delayed_sip", "sip_delayed", 17),
])
def test_unchanged_research_bar_preserves_observation_but_correction_does_not(db, provider, feed, first):
    ingest(db, 0, observed=first, provider=provider, feed=feed)
    ingest(db, 0, observed=40, provider=provider, feed=feed)
    row = db.query(IntradayBar).one()
    assert _aware_utc(row.ingested_at) == START+timedelta(minutes=first)
    ingest(db, 0, observed=41, close=102, provider=provider, feed=feed)
    db.refresh(row)
    assert _aware_utc(row.ingested_at) == START+timedelta(minutes=41)
    assert float(row.close) == 102


def test_unchanged_refresh_after_restart_retains_on_time_outcome(db):
    seed(db)
    run(db, 7)
    ingest(db, 8, observed=10, close=100)
    ingest(db, 15, observed=17)
    # Collector re-fetches the same bars before the learner resumes.
    ingest(db, 8, observed=32, close=100)
    ingest(db, 15, observed=32)
    run(db, 32)
    row = db.query(OnlineResearchForecast).one()
    assert row.status == "scored"
    assert row.outcome["shadow"]["status"] == "observed"


def test_late_correction_cannot_inherit_original_observation_time(db):
    seed(db)
    run(db, 7)
    ingest(db, 15, observed=17)
    ingest(db, 15, observed=32, close=102)
    run(db, 32)
    assert db.query(OnlineResearchForecast).one().status == "expired"


def test_tradier_execution_feed_keeps_existing_refresh_semantics(db):
    ingest(db, 0, observed=2, provider="tradier", feed="sip")
    ingest(db, 0, observed=3, provider="tradier", feed="sip")
    assert _aware_utc(db.query(IntradayBar).one().ingested_at) == START+timedelta(minutes=3)
