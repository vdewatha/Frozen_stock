from copy import deepcopy

from app.models import OnlineResearchForecast
from app.services.online_research import online_research_status
from tests.test_online_research import db, seed, run, add_bar


def spy(db):
    return next(s for s in online_research_status(db)["symbols"] if s["symbol"] == "SPY")["strategy_comparison"]


def test_predeclared_forecasts_are_scored_on_identical_future_outcomes(db):
    seed(db)
    run(db, 7)
    row = db.query(OnlineResearchForecast).one()
    declaration = deepcopy(row.prediction["comparators"])
    assert declaration["probabilities"]["momentum_5m"] == 0.6
    assert declaration["probabilities"]["reversal_5m"] == 0.4
    assert spy(db)["paired_observations"] == 0
    add_bar(db, 15, 101)
    run(db, 17)
    assert row.prediction["comparators"] == declaration
    comparison = spy(db)
    assert comparison["paired_observations"] == 1
    scores = {s["name"]: s["brier"] for s in comparison["strategies"]}
    assert scores["momentum_5m"] < scores["neutral"] < scores["reversal_5m"]
    assert not comparison["profitability_proven"] and not comparison["execution_eligible"]
    run(db, 17)
    assert spy(db) == comparison


def test_legacy_forecasts_are_not_retroactively_compared(db):
    seed(db)
    run(db, 7)
    row = db.query(OnlineResearchForecast).one()
    row.prediction = {k: v for k, v in row.prediction.items() if k != "comparators"}
    db.commit()
    add_bar(db, 15, 101)
    run(db, 17)
    assert row.status == "scored" and "comparators" not in row.outcome
    assert spy(db)["paired_observations"] == 0


def test_expired_targets_do_not_become_strategy_evidence(db):
    seed(db)
    run(db, 7)
    run(db, 31)
    assert spy(db)["paired_observations"] == 0


def test_changed_or_unpaired_metrics_are_excluded(db):
    seed(db)
    run(db, 7)
    add_bar(db, 15, 101)
    run(db, 17)
    row = db.query(OnlineResearchForecast).one()
    outcome = deepcopy(row.outcome)
    outcome["comparators"]["scores"]["momentum_5m"]["brier"] = 0
    row.outcome = outcome
    db.commit()
    assert spy(db)["paired_observations"] == 0


def test_comparisons_do_not_cross_symbols(db):
    seed(db)
    run(db, 7)
    add_bar(db, 15, 101)
    run(db, 17)
    for symbol in online_research_status(db)["symbols"]:
        assert symbol["strategy_comparison"]["paired_observations"] == (1 if symbol["symbol"] == "SPY" else 0)
