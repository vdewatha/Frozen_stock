from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from app.models import IntradayBar, OnlineResearchForecast
from app.services.online_research import online_research_status
from app.services.online_shadow_returns import plan, scores, observe, summarize
from tests.test_online_research import db, seed, run, add_bar, START


def report(db):
    return next(row for row in online_research_status(db)["symbols"] if row["symbol"] == "SPY")["shadow_returns"]


def complete(db, *, entry=100, exit=101):
    seed(db)
    run(db, 7)
    add_bar(db, 8, entry)
    add_bar(db, 15, exit)
    run(db, 17)
    return db.query(OnlineResearchForecast).one()


def test_forward_entry_and_two_sided_cost_stress(db):
    row = complete(db)
    declaration = row.prediction["shadow"]
    assert declaration["entry_at"] == (START + timedelta(minutes=8)).isoformat()
    assert declaration["actions"]["momentum_5m"] == 1
    assert declaration["actions"]["reversal_5m"] == 0
    result = report(db)
    assert result["paired_observations"] == 1
    strategies = {item["name"]: item for item in result["strategies"]}
    long = strategies["momentum_5m"]
    assert long["mean_gross_bps"] == pytest.approx(100)
    assert long["mean_net_bps_by_cost"]["10"] == pytest.approx(79.9)
    assert strategies["reversal_5m"]["mean_net_bps_by_cost"]["10"] == 0
    assert result["overlapping_windows"] and result["research_only"]
    assert not result["broker_fills"] and not result["profitability_proven"]
    assert not result["execution_eligible"]


def test_prices_before_forecast_are_not_entry_prices(db):
    complete(db, entry=110, exit=101)
    long = next(item for item in report(db)["strategies"] if item["name"] == "momentum_5m")
    assert long["mean_gross_bps"] < 0  # Source close was 100.5, but not an available entry.


@pytest.mark.parametrize("ingest", [9-0.5, 25, 40])
def test_unfinished_late_or_future_entry_ingestion_is_unavailable(db, ingest):
    seed(db)
    run(db, 7)
    add_bar(db, 8, ingest=START + timedelta(minutes=ingest))
    add_bar(db, 15, 101)
    run(db, 17)
    assert report(db)["paired_observations"] == 0
    assert report(db)["unavailable_observations"] == 1


def test_no_nearest_entry_or_retroactive_repair(db):
    seed(db)
    run(db, 7)
    add_bar(db, 9, 100)
    add_bar(db, 15, 101)
    run(db, 17)
    original = deepcopy(report(db))
    assert original["unavailable_observations"] == 1
    add_bar(db, 8, 100)
    run(db, 18)
    assert report(db) == original


@pytest.mark.parametrize("entry", [0, -1])
def test_invalid_entry_does_not_create_returns(db, entry):
    complete(db, entry=entry)
    assert report(db)["paired_observations"] == 0
    assert report(db)["unavailable_observations"] == 1


def test_legacy_forecasts_never_gain_economic_declarations(db):
    seed(db)
    run(db, 7)
    row = db.query(OnlineResearchForecast).one()
    row.prediction = {k: v for k, v in row.prediction.items() if k != "shadow"}
    db.commit()
    add_bar(db, 8, 100)
    add_bar(db, 15, 101)
    run(db, 17)
    assert "shadow" not in row.outcome
    assert report(db)["declared_scored_forecasts"] == 0


@pytest.mark.parametrize("change", ["action", "score", "time", "exit"])
def test_inconsistent_evidence_is_excluded(db, change):
    row = complete(db)
    prediction, outcome = deepcopy(row.prediction), deepcopy(row.outcome)
    if change == "action":
        prediction["shadow"]["actions"]["momentum_5m"] = 0
    elif change == "score":
        outcome["shadow"]["scores"]["momentum_5m"]["gross_bps"] = 10000
    elif change == "time":
        outcome["shadow"]["entry_ingested_at"] = START.isoformat()
    else:
        outcome["shadow"]["exit_close"] = 999
    row.prediction, row.outcome = prediction, outcome
    db.commit()
    assert report(db)["paired_observations"] == 0
    assert report(db)["invalid_observations"] == 1


def test_appended_future_bars_cannot_change_forecast_or_action(db):
    seed(db)
    run(db, 7)
    original = deepcopy(db.query(OnlineResearchForecast).one().prediction)
    db.query(OnlineResearchForecast).delete()
    add_bar(db, 8, 500)
    add_bar(db, 15, 1)
    run(db, 7)
    assert db.query(OnlineResearchForecast).one().prediction == original


def test_scored_proxy_snapshot_is_not_rewritten_by_corrected_bars(db):
    row = complete(db)
    original = deepcopy(row.outcome)
    db.query(IntradayBar).filter_by(opened_at=START+timedelta(minutes=8)).update({"open": 500})
    run(db, 18)
    assert row.outcome == original
    assert report(db)["paired_observations"] == 1


def test_empty_and_other_symbols_do_not_fabricate_returns(db):
    assert report(db)["strategies"] == []
    complete(db)
    for symbol in online_research_status(db)["symbols"]:
        assert symbol["shadow_returns"]["paired_observations"] == (1 if symbol["symbol"] == "SPY" else 0)


def test_threshold_is_fixed_and_costs_can_turn_flat_prices_into_loss():
    declaration = plan(START, START+timedelta(minutes=10), {"a": .55, "b": .55001})
    result = scores(declaration, 100, 100)
    assert result["a"]["action"] == "cash"
    assert result["b"]["net_bps_by_cost"]["5"] == -10
    assert result["b"]["gross_bps"] == 0


@pytest.mark.parametrize("entry,exit", [(1e-308, 1.), (1., 1e308)])
def test_finite_prices_cannot_produce_nonfinite_bps(entry, exit):
    declaration = plan(START, START+timedelta(minutes=10), {"a": .9, "b": .1})
    with pytest.raises(ValueError, match="Invalid shadow return"):
        scores(declaration, entry, exit)
    bar = SimpleNamespace(opened_at=START+timedelta(minutes=1),
                          ingested_at=START+timedelta(minutes=2), open=entry)
    observation = observe(declaration, bar, exit, now=START+timedelta(minutes=12))
    assert observation["status"] == "unavailable"
    json.dumps(observation, allow_nan=False)


def test_large_finite_returns_have_a_finite_report_mean():
    declaration = plan(START, START+timedelta(minutes=10), {"a": .9})
    bar = SimpleNamespace(opened_at=START+timedelta(minutes=1),
                          ingested_at=START+timedelta(minutes=2), open=1.)
    now = START+timedelta(minutes=12)
    observation = observe(declaration, bar, 1e304, now=now)
    row = SimpleNamespace(issued_at=START, target_at=START+timedelta(minutes=10),
        prediction={"shadow": declaration, "comparators": {"probabilities": {"a": .9}}},
        outcome={"shadow": observation, "close": 1e304, "observed_at": now.isoformat()})
    result = summarize([row, row, row])
    assert result["paired_observations"] == 3
    assert result["strategies"][0]["mean_gross_bps"] == pytest.approx(1e308)
    json.dumps(result, allow_nan=False)
