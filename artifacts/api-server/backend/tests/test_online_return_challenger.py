from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from app.models import OnlineResearchForecast
from app.services import online_return_challenger as challenger
from app.services import online_shadow_returns as shadow
from app.services.online_research import online_research_status
from tests.test_online_research import db, START, seed, run, add_bar
from tests.test_online_shadow_returns import complete


def history(db, **kwargs):
    row = complete(db, **kwargs)
    return [SimpleNamespace(id=i, symbol=row.symbol, version=row.version, status=row.status,
                            issued_at=row.issued_at, target_at=row.target_at,
                            prediction=deepcopy(row.prediction), outcome=deepcopy(row.outcome))
            for i in range(50)]


def predict(rows, now=START + timedelta(minutes=30)):
    return challenger.predict(rows, [0, 0, 0], symbol="SPY", now=now)


def report(db):
    return next(r for r in online_research_status(db)["symbols"] if r["symbol"] == "SPY")["return_challenger"]


def test_warmup_and_forward_only_enrollment(db):
    row = complete(db)
    assert row.prediction["return_challenger"]["action"] == "cash"
    result = report(db)
    assert result["paired_observations"] == 1
    assert result["mae_bps"] == pytest.approx(100)
    assert result["strategies"][0]["mean_net_bps"] == 0
    assert result["strategies"][1]["mean_net_bps"] == pytest.approx(89.95)
    assert not result["execution_eligible"] and not result["profitability_proven"]
    row.prediction = {k: v for k, v in row.prediction.items() if k != "return_challenger"}
    db.commit()
    run(db, 18)
    assert report(db)["declared_scored_forecasts"] == 0


def test_return_model_learns_magnitude_and_applies_cost_hurdle(db):
    rows = history(db)
    result = predict(rows)
    assert result["training_examples"] == 50
    assert result["predicted_gross_bps"] == pytest.approx(100)
    assert result["action"] == "long"
    assert predict(rows[:49])["action"] == "cash"
    assert predict(rows[::-1]) == result


@pytest.mark.parametrize("exit,action", [(99, "cash"), (100.05, "cash"), (100.2, "long")])
def test_direction_alone_is_not_enough(db, exit, action):
    assert predict(history(db, exit=exit))["action"] == action


@pytest.mark.parametrize("change", ["future", "symbol", "version", "features", "score", "missing"])
def test_invalid_or_future_training_is_excluded(db, change):
    rows = history(db)
    for row in rows:
        if change == "future":
            row.outcome["observed_at"] = (START + timedelta(days=1)).isoformat()
        elif change == "symbol":
            row.symbol = "AAPL"
        elif change == "version":
            row.version = "other"
        elif change == "features":
            row.prediction["features"] = [float("nan"), 0, 0]
        elif change == "score":
            row.outcome["shadow"]["scores"]["always_long"]["gross_bps"] = 99999
        else:
            row.outcome.pop("shadow")
    assert predict(rows) == predict([])


def test_unobserved_outcome_does_not_enter_training(db):
    rows = history(db)
    assert predict(rows, now=START + timedelta(minutes=16)) == predict([], now=START + timedelta(minutes=16))


def test_trained_declaration_is_frozen_and_new_forecast_uses_old_outcome(db):
    row = complete(db)
    frozen = deepcopy(row.prediction)
    for minute in range(16, 22):
        add_bar(db, minute, 101)
    run(db, 23)
    next_row = db.query(OnlineResearchForecast).order_by(OnlineResearchForecast.id.desc()).first()
    assert next_row.prediction["return_challenger"]["training_examples"] == 1
    assert row.prediction == frozen


def test_missing_entry_cannot_be_repaired_into_challenger_evidence(db):
    seed(db)
    run(db, 7)
    add_bar(db, 15, 101)
    run(db, 17)
    assert report(db)["unavailable_observations"] == 1
    add_bar(db, 8, 100)
    run(db, 18)
    assert report(db)["paired_observations"] == 0


@pytest.mark.parametrize("key,value", [("action", "long"), ("cost_bps_per_side", 0),
    ("predicted_gross_bps", float("nan")), ("symbol", "AAPL"), ("training_sha256", "bad")])
def test_mutated_declaration_cannot_claim_performance(db, key, value):
    row = complete(db)
    prediction = deepcopy(row.prediction)
    prediction["return_challenger"][key] = value
    row.prediction = prediction
    db.commit()
    assert report(db)["paired_observations"] == 0
    assert report(db)["invalid_observations"] == 1


def test_future_prices_cannot_change_new_declaration(db):
    seed(db)
    run(db, 7)
    original = deepcopy(db.query(OnlineResearchForecast).one().prediction)
    db.query(OnlineResearchForecast).delete()
    add_bar(db, 8, 500)
    add_bar(db, 15, 1)
    run(db, 7)
    assert db.query(OnlineResearchForecast).one().prediction == original


def test_nonfinite_return_is_excluded_before_training_clip(db):
    rows = history(db)
    for row in rows:
        row.outcome["close"] = row.outcome["shadow"]["exit_close"] = 1e308
        row.outcome["shadow"]["entry_open"] = 1.
        for score in row.outcome["shadow"]["scores"].values():
            score["gross_bps"] = float("inf") if score["action"] == "long" else 0.
            score["net_bps_by_cost"] = {str(c): score["gross_bps"] for c in shadow.COST_BPS}
    assert predict(rows) == predict([])


def test_large_valid_return_does_not_overflow_challenger_report(db):
    rows = history(db)
    for row in rows:
        observation = row.outcome["shadow"]
        row.outcome["close"] = observation["exit_close"] = 1e304
        observation["entry_open"] = 1.
        observation["scores"] = shadow.scores(row.prediction["shadow"], 1., 1e304)
    result = challenger.summarize(rows, symbol="SPY", now=START+timedelta(minutes=30))
    assert result["paired_observations"] == 50
    assert result["mae_bps"] == pytest.approx(1e308)
    json.dumps(result, allow_nan=False)

    for row in rows:
        row.prediction["return_challenger"].update(training_examples=50, predicted_gross_bps=-1e308)
    result = challenger.summarize(rows, symbol="SPY", now=START+timedelta(minutes=30))
    assert result["invalid_observations"] == 50
    assert result["paired_observations"] == 0
    json.dumps(result, allow_nan=False)
