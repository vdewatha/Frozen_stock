from datetime import datetime, timedelta, timezone
from copy import deepcopy

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import IntradayBar, OnlineResearchForecast
from app.services.online_research import advance_online_research, online_research_status

START = datetime(2026, 9, 28, 14, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def add_bar(db, minute, close=100, provider="alpaca_iex", feed="iex", ingest=None):
    at = START + timedelta(minutes=minute)
    db.add(IntradayBar(symbol="SPY", timeframe="1m", opened_at=at,
                       open=close, high=close, low=close, close=close, volume=100,
                       provider=provider, feed_class=feed, exchange_timestamp=at,
                       ingested_at=ingest or at + timedelta(minutes=2)))
    db.flush()


def seed(db):
    for minute in range(6):
        add_bar(db, minute, 100 + minute/10)


def run(db, minute):
    result = advance_online_research(db, now=START + timedelta(minutes=minute))
    db.commit()
    return result


def test_prediction_precedes_outcome_and_replay_is_idempotent(db):
    seed(db)
    run(db, 7)
    run(db, 7)
    row = db.query(OnlineResearchForecast).one()
    assert row.status == "pending"
    assert row.issued_at < row.target_at
    frozen = dict(row.prediction)
    assert frozen["training_examples"] == 0
    add_bar(db, 15, 101)
    run(db, 16)
    assert row.status == "pending"
    run(db, 17)
    assert row.status == "scored"
    assert row.outcome["label"] == 1
    assert row.outcome["model"]["brier"] == 0.25
    add_bar(db, 16, 101)
    for minute in range(17, 22):
        add_bar(db, minute, 101)
    run(db, 23)
    rows = db.query(OnlineResearchForecast).order_by(OnlineResearchForecast.id).all()
    assert len(rows) == 2
    assert rows[0].prediction == frozen
    assert rows[1].prediction["training_examples"] == 1
    assert rows[1].prediction["probability_up"] != 0.5
    run(db, 23)
    assert db.query(OnlineResearchForecast).count() == 2
    assert online_research_status(db)["execution_eligible"] is False


@pytest.mark.parametrize("minute", [6, 9, 400])
def test_no_forecasts_from_unfinished_stale_or_closed_session(db, minute):
    seed(db)
    run(db, minute)
    assert db.query(OnlineResearchForecast).count() == 0


def test_missing_target_expires_never_uses_nearest_bar_or_late_backfill(db):
    seed(db)
    run(db, 7)
    add_bar(db, 16, 110)
    run(db, 31)
    row = db.query(OnlineResearchForecast).one()
    assert row.status == "expired"
    add_bar(db, 15, 101)
    run(db, 32)
    assert row.status == "expired"
    assert row.outcome is None


def test_backfill_after_deadline_cannot_score_pending(db):
    seed(db)
    run(db, 7)
    add_bar(db, 15, 101, ingest=START + timedelta(minutes=32))
    run(db, 32)
    assert db.query(OnlineResearchForecast).one().status == "expired"


def test_feed_isolation_and_missing_minutes(db):
    for minute in range(6):
        add_bar(db, minute, provider="tradier", feed="sip")
    run(db, 7)
    assert db.query(OnlineResearchForecast).count() == 0
    for minute in (0, 1, 2, 4, 5, 6):
        add_bar(db, minute)
    run(db, 8)
    assert db.query(OnlineResearchForecast).count() == 0


def test_future_ingestion_rejected(db):
    seed(db)
    db.query(IntradayBar).update({"ingested_at": START + timedelta(minutes=20)})
    run(db, 7)
    assert db.query(OnlineResearchForecast).count() == 0


def test_naive_clock_rejected(db):
    with pytest.raises(ValueError):
        advance_online_research(db, now=START.replace(tzinfo=None))


@pytest.mark.parametrize("bad", [None, [], {},
    {"source_close": "bad"}, {"probability_up": 2},
    {"baseline_up": True}, {"features": [1, 2]}])
def test_invalid_pending_forecast_does_not_block_valid_scoring(db, bad):
    seed(db)
    run(db, 7)
    broken = db.query(OnlineResearchForecast).one()
    valid_prediction = deepcopy(broken.prediction)
    valid = OnlineResearchForecast(symbol="AAPL", version=broken.version,
        source_at=broken.source_at, issued_at=broken.issued_at,
        target_at=broken.target_at, status="pending", prediction=valid_prediction)
    db.add(valid)
    broken.prediction = ({**valid_prediction, **bad} if isinstance(bad, dict) and bad else bad)
    frozen = deepcopy(broken.prediction)
    add_bar(db, 15, 101)
    bar = db.query(IntradayBar).filter_by(symbol="SPY", opened_at=START+timedelta(minutes=15)).one()
    db.add(IntradayBar(symbol="AAPL", timeframe="1m", opened_at=bar.opened_at,
        open=101, high=101, low=101, close=101, volume=100,
        provider="alpaca_iex", feed_class="iex", exchange_timestamp=bar.opened_at,
        ingested_at=bar.ingested_at))
    run(db, 17)
    assert broken.status == "invalid"
    assert broken.prediction == frozen
    assert broken.outcome is None
    assert valid.status == "scored"
    run(db, 18)
    report = online_research_status(db)
    spy = next(item for item in report["symbols"] if item["symbol"] == "SPY")
    assert spy["invalid_forecasts"] == 1
    assert spy["scored"] == 0
    assert spy["metric_window"] == 0


@pytest.mark.parametrize("observed", ["2026-09-28T14:30:00+00:00", "2026-09-28T13:50:00", "invalid"])
def test_training_never_uses_future_or_unverifiable_outcomes(db, observed):
    seed(db)
    db.add(OnlineResearchForecast(symbol="SPY", version="iex-sgd-v1",
        source_at=START-timedelta(minutes=30), issued_at=START-timedelta(minutes=28),
        target_at=START-timedelta(minutes=20), status="scored",
        prediction={"features": [1, 1, 1]}, outcome={"label": 1, "observed_at": observed}))
    db.commit()
    run(db, 7)
    forecast = db.query(OnlineResearchForecast).filter_by(status="pending").one()
    assert forecast.prediction["training_examples"] == 0
    assert forecast.prediction["probability_up"] == 0.5


def test_source_bar_cannot_be_ingested_before_it_finishes(db):
    seed(db)
    bar = db.query(IntradayBar).order_by(IntradayBar.opened_at.desc()).first()
    bar.ingested_at = bar.opened_at + timedelta(seconds=30)
    run(db, 7)
    assert db.query(OnlineResearchForecast).count() == 0


def test_invalid_target_expires_instead_of_remaining_pending_forever(db):
    seed(db)
    run(db, 7)
    add_bar(db, 15, close=0)
    run(db, 31)
    row = db.query(OnlineResearchForecast).one()
    assert row.status == "expired" and row.outcome is None


def test_status_compares_against_neutral_probability_baseline(db):
    seed(db)
    run(db, 7)
    add_bar(db, 15, 101)
    run(db, 17)
    status = next(row for row in online_research_status(db)["symbols"] if row["symbol"] == "SPY")
    assert status["neutral_brier"] == 0.25
    assert status["beats_neutral_baseline"] is False


@pytest.mark.parametrize("damage", ["label", "features", "probability", "loss", "missing_loss", "close"])
def test_invalid_scored_evidence_is_not_reported_or_used_for_training(db, damage):
    seed(db)
    run(db, 7)
    add_bar(db, 15, 101)
    run(db, 17)
    row = db.query(OnlineResearchForecast).one()
    prediction, outcome = deepcopy(row.prediction), deepcopy(row.outcome)
    if damage == "label":
        outcome["label"] = 0
    elif damage == "features":
        prediction["features"] = [1, 2]
    elif damage == "probability":
        prediction["probability_up"] = 2
    elif damage == "loss":
        outcome["model"]["brier"] = 0
    elif damage == "missing_loss":
        outcome.pop("model")
    else:
        outcome["close"] = -1
    row.prediction, row.outcome = prediction, outcome
    db.commit()
    status = next(r for r in online_research_status(db)["symbols"] if r["symbol"] == "SPY")
    assert status["brier"] is None
    assert status["metric_window"] == 0
    assert status["invalid_scored_observations"] == 1
    assert status["strategy_comparison"]["paired_observations"] == 0
    for minute in range(16, 22):
        add_bar(db, minute, 101)
    run(db, 23)
    forecast = db.query(OnlineResearchForecast).filter_by(status="pending").one()
    assert forecast.prediction["training_examples"] == 0
    assert forecast.prediction["probability_up"] == .5
    assert row.outcome == outcome
