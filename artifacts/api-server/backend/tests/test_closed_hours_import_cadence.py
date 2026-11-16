"""Deep in closed hours the intraday import runs every eight minutes, not every minute."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
import app.models.stock_paper  # noqa: F401
from app.services import intraday_data
from app.services.intraday_data import (
    CLOSED_HOURS_VERIFICATION_MAX_AGE,
    ENTITLEMENT_VERIFICATION_MAX_AGE,
    closed_hours_import_wait,
    deep_closed_hours,
    entitlement_max_age,
)
from app.tasks import jobs

UTC = timezone.utc
# Friday 2026-09-11: regular session 13:30-20:00 UTC. Monday 2026-09-14 opens 13:30 UTC.
OPEN = datetime(2026, 9, 11, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 9, 11, 20, 0, tzinfo=UTC)
MONDAY_OPEN = datetime(2026, 9, 14, 13, 30, tzinfo=UTC)


def test_deep_closed_hours_boundaries():
    cases = {
        OPEN - timedelta(hours=3): True,                       # early morning, well before the open
        OPEN - timedelta(minutes=61): True,
        OPEN - timedelta(minutes=60): True,
        OPEN - timedelta(minutes=59): False,                   # full pace for the hour before the open
        OPEN: False,
        OPEN + timedelta(hours=3): False,                      # in session
        CLOSE: False,
        CLOSE + timedelta(minutes=119): False,                 # repair window after the close
        CLOSE + timedelta(hours=2): True,
        datetime(2026, 9, 12, 15, 0, tzinfo=UTC): True,        # Saturday
        datetime(2026, 9, 13, 15, 0, tzinfo=UTC): True,        # Sunday
        MONDAY_OPEN - timedelta(minutes=30): False,
    }
    for moment, expected in cases.items():
        assert deep_closed_hours(moment) is expected, moment


def test_verification_window_is_five_minutes_whenever_trading_is_near():
    for moment in (OPEN, OPEN + timedelta(hours=2), CLOSE + timedelta(minutes=30),
                   OPEN - timedelta(minutes=44), MONDAY_OPEN - timedelta(minutes=10)):
        assert entitlement_max_age(moment) == ENTITLEMENT_VERIFICATION_MAX_AGE, moment
    for moment in (CLOSE + timedelta(hours=3), datetime(2026, 9, 12, 15, 0, tzinfo=UTC),
                   # the wider window outlasts the slow pace by a quarter of an hour
                   OPEN - timedelta(minutes=50)):
        assert entitlement_max_age(moment) == CLOSED_HOURS_VERIFICATION_MAX_AGE, moment


def run(finished_ago: timedelta, status: str = "complete", *, at: datetime) -> dict:
    return {"status": status, "finished_at": (at - finished_ago).isoformat(), "duration_seconds": 80.0}


def test_import_waits_only_deep_in_closed_hours_after_a_finished_run():
    night = CLOSE + timedelta(hours=5)
    assert closed_hours_import_wait(night, run(timedelta(minutes=3), at=night)) == 300.0
    assert closed_hours_import_wait(night, run(timedelta(minutes=3), "incomplete", at=night)) == 300.0
    assert closed_hours_import_wait(night, run(timedelta(minutes=8), at=night)) is None          # due
    assert closed_hours_import_wait(night, run(timedelta(minutes=3), "failed", at=night)) is None  # retry at once
    assert closed_hours_import_wait(night, None) is None                                         # no record
    assert closed_hours_import_wait(night, {"status": "complete", "finished_at": "not a time"}) is None
    assert closed_hours_import_wait(night, run(timedelta(minutes=-2), at=night)) is None         # clock skew
    session = OPEN + timedelta(hours=1)
    assert closed_hours_import_wait(session, run(timedelta(seconds=20), at=session)) is None
    before_open = OPEN - timedelta(minutes=30)
    assert closed_hours_import_wait(before_open, run(timedelta(seconds=20), at=before_open)) is None


def test_the_slow_pace_keeps_the_proof_inside_its_window_through_two_failed_runs():
    # A run stamps bars with its start time and takes up to two minutes; the next
    # starts eight minutes after it finishes. Two runs in a row may fail.
    cycle = intraday_data.CLOSED_HOURS_IMPORT_GAP + timedelta(minutes=2)
    oldest_proof = 3 * cycle + timedelta(minutes=2)
    assert oldest_proof <= CLOSED_HOURS_VERIFICATION_MAX_AGE


def test_scheduled_job_skips_without_touching_the_database():
    night = CLOSE + timedelta(hours=5)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return night if tz else night.replace(tzinfo=None)

    with patch.object(jobs, "datetime", Clock), \
         patch("app.services.operations_health.latest_intraday_import_run",
               return_value=run(timedelta(minutes=2), at=night)), \
         patch.object(jobs, "_run_job", side_effect=AssertionError("the job must not start")):
        result = jobs.intraday_market_data_import()
    assert result["status"] == "skipped"
    assert result["next_run_in_seconds"] == 360


def test_scheduled_job_runs_when_due_or_when_the_market_is_near():
    for moment, last in (
        (CLOSE + timedelta(hours=5), run(timedelta(minutes=9), at=CLOSE + timedelta(hours=5))),
        (CLOSE + timedelta(hours=5), None),
        (OPEN + timedelta(hours=1), run(timedelta(seconds=30), at=OPEN + timedelta(hours=1))),
    ):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None, _moment=moment):
                return _moment if tz else _moment.replace(tzinfo=None)

        with patch.object(jobs, "datetime", Clock), \
             patch("app.services.operations_health.latest_intraday_import_run", return_value=last), \
             patch.object(jobs, "_run_job", return_value={"status": "complete"}) as started:
            assert jobs.intraday_market_data_import() == {"status": "complete"}
        assert started.call_count == 1


def bar(opened_at: datetime) -> dict:
    return {"t": opened_at.isoformat().replace("+00:00", "Z"), "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 300}


def test_feed_status_accepts_an_older_proof_only_deep_in_closed_hours():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        with patch.object(settings, "active_market_data_provider", "alpaca_iex"), \
             patch.object(intraday_data, "MARKET_DATA_PROVIDER", "alpaca_iex"), \
             patch.object(intraday_data, "MARKET_DATA_FEED_CLASS", "iex"), \
             patch.object(settings, "paper_alpaca_api_key", type(settings.paper_alpaca_api_key)("key")), \
             patch.object(settings, "paper_alpaca_api_secret", type(settings.paper_alpaca_api_secret)("secret")):
            verified = CLOSE + timedelta(minutes=10)
            intraday_data.upsert_intraday_bars(
                db, "SPY", [bar(OPEN + timedelta(minutes=index)) for index in range(390)],
                ingested_at=verified, verify_entitlement=True,
            )
            db.commit()

            def state(at: datetime) -> str:
                return intraday_data.feed_status(db, "SPY", now=at)["entitlement_state"]

            assert state(verified + timedelta(minutes=4)) == "verified"
            assert state(verified + timedelta(minutes=20)) == "unverified"      # still inside the repair window
            night = CLOSE + timedelta(hours=4)
            intraday_data.upsert_intraday_bars(
                db, "SPY", [bar(CLOSE - timedelta(minutes=1))], ingested_at=night, verify_entitlement=True,
            )
            db.commit()
            assert state(night + timedelta(minutes=20)) == "verified"
            assert state(night + timedelta(minutes=34)) == "verified"
            assert state(night + timedelta(minutes=36)) == "unverified"
    finally:
        db.close()
        engine.dispose()
