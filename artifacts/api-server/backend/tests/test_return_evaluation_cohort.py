from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.models import OnlineResearchForecast
from app.services import return_evaluation_cohort as cohort
from app.services.online_research import online_research_status
from tests.test_online_research import START, db, seed, run, add_bar


def row(id, minute, status="pending"):
    issued = START + timedelta(minutes=minute)
    target = issued + timedelta(minutes=8)
    return SimpleNamespace(id=id, issued_at=issued, target_at=target, status=status,
        symbol="SPY", version="iex-sgd-v1", outcome=None,
        prediction={"return_challenger": {}, "return_evaluation_cohort": cohort.plan(issued, target)})


def report(rows):
    return cohort.report(rows, symbol="SPY", now=START + timedelta(hours=1))


def test_fixed_slots_do_not_overlap_and_off_slot_is_excluded():
    rows = [row(i, i) for i in range(30)]
    result = report(rows)
    assert result["selected_forecasts"] == 3
    assert result["excluded_off_slot"] == 27
    assert result["pending"] == 3
    assert result["paired_observations"] == 0
    assert not result["overlapping_windows"]
    assert not result["independence_proven"]
    assert not result["execution_eligible"]
    assert report(rows[::-1]) == result


def test_expired_slot_is_not_replaced_by_overlapping_success():
    first, later = row(1, 0, "expired"), row(2, 1, "scored")
    result = report([first, later])
    assert result["selected_forecasts"] == 1
    assert result["expired"] == 1
    assert result["paired_observations"] == 0


def test_duplicate_slot_cannot_replace_first_missing_outcome():
    first, second = row(1, 0, "expired"), row(2, 0)
    result = report([second, first])
    assert result["expired"] == 1
    assert result["pending"] == 0
    assert result["invalid_declarations"] == 1


@pytest.mark.parametrize("change", ["symbol", "version", "slot", "inclusion"])
def test_invalid_declarations_cannot_enter_cohort(change):
    r = row(1, 0)
    if change == "symbol":
        r.symbol = "AAPL"
    elif change == "version":
        r.version = "other"
    elif change == "slot":
        r.prediction["return_evaluation_cohort"]["slot_at"] = START.isoformat()+"bad"
    else:
        r.prediction["return_evaluation_cohort"]["included"] = False
    result = report([r])
    assert result["selected_forecasts"] == 0
    assert result["invalid_declarations"] == 1


def test_legacy_not_retroactively_enrolled(db):
    seed(db)
    run(db, 7)
    r = db.query(OnlineResearchForecast).one()
    frozen = deepcopy(r.prediction)
    assert frozen["return_evaluation_cohort"]["included"] is False
    r.prediction = {k: v for k, v in frozen.items() if k != "return_evaluation_cohort"}
    db.commit()
    run(db, 8)
    assert "return_evaluation_cohort" not in r.prediction
    result = next(s for s in online_research_status(db)["symbols"] if s["symbol"] == "SPY")
    assert result["return_challenger"]["nonoverlapping"]["selected_forecasts"] == 0


def test_target_must_finish_before_next_slot():
    assert cohort.plan(START, START+timedelta(minutes=9))["included"]
    assert not cohort.plan(START, START+timedelta(minutes=10))["included"]
    assert not cohort.plan(START, START)["included"]


@pytest.mark.parametrize("entry_present", [True, False])
def test_actual_forecast_cohort_scores_or_preserves_missing(db, entry_present):
    for minute in range(3, 9):
        add_bar(db, minute, 100)
    run(db, 10)
    if entry_present:
        add_bar(db, 11, 100)
    add_bar(db, 18, 101)
    run(db, 20)
    result = next(s for s in online_research_status(db)["symbols"] if s["symbol"] == "SPY")
    sample = result["return_challenger"]["nonoverlapping"]
    assert sample["selected_forecasts"] == 1
    assert sample["pending"] == 0
    assert sample["paired_observations"] == int(entry_present)
    assert sample["unavailable_observations"] == int(not entry_present)
    if entry_present:
        assert sample["strategies"][1]["mean_net_bps"] == pytest.approx(89.95)
    else:
        add_bar(db, 11, 100)
        run(db, 21)
        assert cohort.report(db.query(OnlineResearchForecast).all(), symbol="SPY",
                             now=START+timedelta(hours=1)) == sample
