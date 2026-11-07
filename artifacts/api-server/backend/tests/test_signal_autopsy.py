"""Signal autopsy: losses vs wins on their decision-bar features, scope candidates, the scope template and the live scope."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from app.core.security import required_role
from app.models import AuditLog, MarketPrice
from app.services import experiment_engine as engine
from app.services import gate_replay
from app.services import signal_autopsy as autopsy
from app.services.audit import write_audit_log
from app.services.strategies import rules
from app.services.strategies.base import StrategySignalResult
from app.services.strategies.registry import get_strategy
from app.services.strategies.scope import ScopedStrategy, apply_scope, scope_condition, scope_mask
from app.tasks.celery_app import celery_app
from tests.test_experiment_engine import _attribution, small_universe  # noqa: F401  (fixture)
from tests.test_loss_feedback_loop import _strategy, db  # noqa: F401  (fixture)
from tests.test_meta_label import _walk

UTC = timezone.utc
NOW = datetime(2026, 11, 8, 16, 30, tzinfo=UTC)  # Sunday 11:30 ET
RULE = {"f": "rsi_2", "op": "<", "v": 10.0}


def _store_bars(db, symbol, frame, *, adjust=1.0):  # noqa: F811
    for _, bar in frame.iterrows():
        db.add(MarketPrice(symbol=symbol, price_date=date.fromisoformat(bar["date"]), open=Decimal(str(bar["open"])),
                           high=Decimal(str(bar["high"])), low=Decimal(str(bar["low"])), close=Decimal(str(bar["close"])),
                           adjusted_close=Decimal(str(bar["close"] * adjust)), volume=int(bar["volume"]), source="test"))
    db.flush()


def _trade(is_loss, **features):
    return {"symbol": "AAPL", "opened_at": None, "closed_at": None, "entry_session": "2026-01-05",
            "is_loss": is_loss, "return": -0.02 if is_loss else 0.03, "cause": "signal_wrong" if is_loss else "edge",
            "features": {name: None for name in autopsy.FEATURES} | features}


# ------------------------------------------------------ decision-bar features

def test_features_are_computed_on_the_bar_before_the_entry_session(db):  # noqa: F811
    frame = _walk(rows=300, seed=7, start="2025-01-02")
    _store_bars(db, "AAPL", frame, adjust=0.5)   # a split: the stored adjusted close is half the raw close
    entry = date(2026, 1, 20)                    # a Tuesday; the decision bar is the stored Monday bar
    row = _attribution(db, strategy_type="channel_breakout", cause="signal_wrong", net_return=-0.02,
                       closed_at=datetime(2026, 1, 23, 20, 0, tzinfo=UTC), opened_at=datetime(2026, 1, 20, 14, 35, tzinfo=UTC))
    row.detail = {"diagnosis": {"cause": "signal_wrong", "entry_vix": 27.5, "entry_breadth": 38.0}}
    db.flush()
    bars = autopsy.symbol_bars(db, "AAPL", entry)
    assert bars["date"].iloc[-1] == "2026-01-19" and bars["date"].iloc[0] == "2025-01-02"
    # The paper frame is scaled by adjusted_close / close; so is this one.
    raw = frame.set_index("date")
    assert bars["close"].iloc[-1] == pytest.approx(raw.loc["2026-01-19", "close"] * 0.5)
    assert bars["high"].iloc[-1] == pytest.approx(raw.loc["2026-01-19", "high"] * 0.5)
    decision = autopsy.decision_frame(bars, entry)
    assert len(decision) == len(bars) and decision["date"].iloc[-1] == "2026-01-19"
    records = autopsy.trade_features(db, [row])
    assert len(records) == 1
    features = records[0]["features"]
    assert features["decision_bar"] == "2026-01-19"
    expected = frame.loc[frame["date"] < "2026-01-20"].reset_index(drop=True)
    for name in autopsy.RULE_FEATURES:
        # Stored prices are Numeric(18, 6): the adjustment round-trips to within 1e-3 of the raw feature.
        assert features[name] == pytest.approx(float(rules.feature(expected, name).iloc[-1]), abs=1e-3), name
    assert features["dow"] == 0.0                   # Monday
    assert features["entry_vix"] == 27.5 and features["entry_breadth"] == 38.0
    assert records[0]["is_loss"] is True and records[0]["cause"] == "signal_wrong"


def test_features_are_none_without_enough_bars(db):  # noqa: F811
    _store_bars(db, "MSFT", _walk(rows=30, seed=2, start="2026-01-02"))
    row = _attribution(db, strategy_type="channel_breakout", cause="edge", net_return=0.02, symbol="MSFT",
                       closed_at=datetime(2026, 3, 1, tzinfo=UTC), opened_at=datetime(2026, 2, 20, 15, 0, tzinfo=UTC))
    features = autopsy.trade_features(db, [row])[0]["features"]
    assert features["dist_sma_200"] is None and features["vol_ratio_20_100"] is None   # not enough history
    assert features["rsi_14"] is not None and features["dow"] == 3.0                    # 2026-02-12 is a Thursday
    assert autopsy.entry_features(autopsy.decision_frame(pd.DataFrame(), date(2026, 1, 1)))["decision_bar"] is None


# ---------------------------------------------------------------- statistics

def test_permutation_test_separates_what_differs_and_not_what_does_not():
    rng = np.random.default_rng(1)
    losses, wins = rng.normal(40, 10, 40), rng.normal(20, 10, 40)
    apart = autopsy.permutation_test(losses, wins, permutations=2000, seed=0)
    assert apart["n_losses"] == 40 and apart["diff"] == pytest.approx(losses.mean() - wins.mean(), abs=1e-5)
    assert apart["effect_size"] > 1.5 and apart["p_value"] < 0.01
    same = autopsy.permutation_test(rng.normal(30, 10, 40), rng.normal(30, 10, 40), permutations=2000, seed=0)
    assert same["p_value"] > 0.10 and abs(same["effect_size"]) < 0.5
    # Too few on a side: the table row is reported, not tested.
    thin = autopsy.permutation_test([1.0], [2.0, 3.0, None, float("nan")], permutations=50)
    assert thin["p_value"] is None and thin["n_wins"] == 2 and thin["median_wins"] == 2.5
    # Deterministic under the seed.
    assert autopsy.permutation_test(losses, wins, permutations=500, seed=3) == autopsy.permutation_test(
        losses, wins, permutations=500, seed=3)


def test_benjamini_hochberg_steps_up_and_skips_untested_features():
    q = autopsy.benjamini_hochberg([0.01, None, 0.04, 0.03, 0.20])
    assert q[1] is None
    # m = 4: ranks 0.01, 0.03, 0.04, 0.20 -> 0.04, min(0.06, 0.0533), 0.0533, 0.20
    assert q[0] == pytest.approx(0.04) and q[3] == pytest.approx(0.053333) and q[2] == pytest.approx(0.053333)
    assert q[4] == pytest.approx(0.20)
    assert autopsy.benjamini_hochberg([None, None]) == [None, None]
    assert autopsy.benjamini_hochberg([0.5]) == [0.5]


def test_contrast_builds_the_table_from_trade_records():
    rng = np.random.default_rng(5)
    trades = [_trade(True, rsi_2=float(v), entry_vix=float(x)) for v, x in zip(rng.normal(40, 8, 30), rng.normal(22, 3, 30))]
    trades += [_trade(False, rsi_2=float(v), entry_vix=float(x)) for v, x in zip(rng.normal(8, 5, 30), rng.normal(22, 3, 30))]
    table = {row["feature"]: row for row in autopsy.contrast(trades, permutations=500, seed=1)}
    assert set(table) == set(autopsy.FEATURES)
    assert table["rsi_2"]["q_value"] < 0.05 and table["rsi_2"]["scope_feature"] is True
    assert table["entry_vix"]["q_value"] > 0.10 and table["entry_vix"]["scope_feature"] is False
    assert table["ibs"]["p_value"] is None and table["ibs"]["q_value"] is None and table["ibs"]["n_losses"] == 0


# ---------------------------------------------------------------- candidates

def _row(feature, *, q, n=20, mean_losses=40.0, mean_wins=10.0, median_wins=9.5, scope=True, d=-1.2):
    return {"feature": feature, "scope_feature": scope, "n_losses": n, "n_wins": n, "mean_losses": mean_losses,
            "mean_wins": mean_wins, "median_wins": median_wins, "median_losses": 41.0, "diff": mean_losses - mean_wins,
            "effect_size": d, "p_value": None if q is None else q / 2, "q_value": q}


def test_candidates_threshold_at_the_winners_median_in_their_direction():
    table = [
        _row("rsi_2", q=0.01, d=1.8),                                                   # wins lower -> "<"
        _row("dist_sma_200", q=0.05, mean_losses=-0.05, mean_wins=0.02, median_wins=0.0123, d=-0.9),  # wins higher -> ">"
        _row("atr_pct_14", q=0.20),                                                     # does not separate
        _row("gap", q=0.01, n=9),                                                       # too few on a side
        _row("entry_vix", q=0.001, scope=False),                                        # not a scope feature
        _row("ibs", q=None),                                                            # untested
    ]
    found = autopsy.candidates(table)
    assert [c["rule"] for c in found] == [{"f": "rsi_2", "op": "<", "v": 9.5},
                                          {"f": "dist_sma_200", "op": ">", "v": 0.0123}]
    assert found[0]["label"] == "rsi_2<9.5" and "decision bar" in found[0]["description"]
    assert "q = 0.010" in found[0]["why"]
    # The engine's scope wrapper accepts every candidate as a valid parameter.
    for candidate in found:
        assert scope_condition({"scope": candidate["rule"]}) == candidate["rule"]
    assert autopsy.candidates([_row(f"ret_{i}", q=0.01) for i in range(1, 9)], limit=3).__len__() == 3


def test_autopsy_payload_counts_the_trigger_and_is_json_safe():
    trades = [_trade(True, rsi_2=np.float64(30.0)) for _ in range(3)] + [_trade(False, rsi_2=np.float64(5.0)) for _ in range(2)]
    trades[0]["cause"] = "missed_market_move"
    payload = autopsy.autopsy("channel_breakout", trades, now=NOW, permutations=100)
    assert payload["trades"] == 5 and payload["losses"] == 3 and payload["wins"] == 2 and payload["signal_wrong_losses"] == 2
    assert payload["candidates"] == [] and payload["paper_only"] and payload["hindsight_only"]
    assert len(payload["trade_records"]) == 5 and payload["method"] == autopsy.METHOD
    import json
    json.dumps(autopsy._clean(payload))


# ------------------------------------------------------------------- ledger

def _closed_trades(db, strategy_type, symbol, bars, losses, wins):  # noqa: F811
    rows = []
    sessions = [date.fromisoformat(d) for d in bars["date"].iloc[220:220 + losses + wins]]
    for i, session in enumerate(sessions):
        opened = datetime(session.year, session.month, session.day, 15, 0, tzinfo=UTC)
        rows.append(_attribution(db, strategy_type=strategy_type, symbol=symbol, cause="signal_wrong" if i < losses else "edge",
                                 net_return=-0.02 if i < losses else 0.03, opened_at=opened,
                                 closed_at=opened + timedelta(days=4)))
    return rows


def test_record_autopsies_writes_one_row_per_strategy_with_enough_trades(db, monkeypatch):  # noqa: F811
    bars = _walk(rows=300, seed=9, start="2025-01-02")
    _store_bars(db, "AAPL", bars)
    row = _strategy(db, "channel_breakout")
    _closed_trades(db, "channel_breakout", "AAPL", bars, losses=6, wins=6)
    _closed_trades(db, "rsi2_pullback", "AAPL", bars, losses=2, wins=1)
    result = autopsy.record_autopsies(db, now=NOW, permutations=100)
    assert result["recorded"] == [{"strategy_type": "channel_breakout", "trades": 12, "losses": 6, "signal_wrong_losses": 6,
                                   "candidates": []}]
    assert result["skipped"][0]["strategy_type"] == "rsi2_pullback" and "fewer than 10" in result["skipped"][0]["reason"]
    audit = db.scalars(select(AuditLog).where(AuditLog.action == autopsy.ACTION)).all()
    assert len(audit) == 1 and audit[0].entity_id == row.id and audit[0].event_type == autopsy.EVENT
    payload = audit[0].payload
    assert payload["strategy_type"] == "channel_breakout" and payload["trades"] == 12
    table = {r["feature"]: r for r in payload["features"]}
    assert table["rsi_2"]["n_losses"] == 6 and table["rsi_2"]["n_wins"] == 6 and table["rsi_2"]["p_value"] is not None
    assert table["entry_vix"]["n_losses"] == 0      # the test attributions carry no entry context
    assert all(r["decision_bar"] < r["entry_session"] for r in payload["trade_records"])
    assert autopsy.latest_autopsy(db, "channel_breakout") == payload and autopsy.latest_autopsy(db, "rsi2_pullback") is None
    view = autopsy.signal_autopsy_view(db)
    assert view["as_of"] == payload["as_of"] and view["strategies"][0]["strategy_type"] == "channel_breakout"
    assert view["live_scopes"] == {} and view["scope_features"] == list(autopsy.RULE_FEATURES) and view["paper_only"]
    # A second run supersedes the first for the engine's feed.
    autopsy.record_autopsies(db, now=NOW + timedelta(days=7), permutations=100)
    assert autopsy.latest_autopsy(db, "channel_breakout")["as_of"] == (NOW + timedelta(days=7)).isoformat()


def _autopsy_row(db, row, candidates):  # noqa: F811
    write_audit_log(db, event_type=autopsy.EVENT, entity_type="strategy", entity_id=row.id, action=autopsy.ACTION,
                    status="complete", message="test autopsy",
                    payload={"strategy_type": row.strategy_type, "as_of": NOW.isoformat(),
                             "candidates": [{"rule": rule, "label": "x"} for rule in candidates]})
    db.flush()


def test_scope_candidates_come_from_the_latest_autopsy_off_first(db):  # noqa: F811
    row = _strategy(db, "channel_breakout")
    assert autopsy.scope_candidates(row) == ()                       # no autopsy yet
    assert autopsy.scope_candidates(object()) == ()                  # a test double
    assert autopsy.scope_candidates(SimpleNamespace(strategy_type="x", parameters={})) == ()
    _autopsy_row(db, row, [RULE, {"f": "nonsense", "op": "<", "v": 1}, {"f": "dist_sma_200", "op": ">", "v": 0.0}])
    assert autopsy.scope_candidates(row) == ({"scope": None}, {"scope": RULE},
                                             {"scope": {"f": "dist_sma_200", "op": ">", "v": 0.0}})
    # A live scope is never its own candidate, and is always tested against off.
    row.parameters = {"scope": RULE}
    db.flush()
    assert autopsy.scope_candidates(row) == ({"scope": None}, {"scope": {"f": "dist_sma_200", "op": ">", "v": 0.0}})
    _autopsy_row(db, row, [])
    assert autopsy.scope_candidates(row) == ({"scope": None},)


# ------------------------------------------------------------ the template

def test_scope_template_is_dynamic_and_static_templates_are_unchanged():
    template = engine.TEMPLATE_BY_NAME["scope"]
    assert template.kind == "scope" and template.triggers == {"signal_wrong"} and template.key == "scope"
    assert template.governance_action == "scope_update" and template.candidates == ()
    assert template.candidates_for("channel_breakout") == ()
    assert template.label({"scope": None}) == "off" and template.label({"scope": RULE}) == "rsi_2<10"
    assert template.describe({"scope": RULE}) == "BUY only when rsi_2 < 10 on the decision bar"
    assert template not in engine.GATE_TEMPLATES and template not in engine.EXIT_TEMPLATES
    for other in engine.TEMPLATES:
        if other.name != "scope":
            assert other.dynamic is None and other.candidates
    # The only dynamic template; the static ones (the lesson-template block included) keep fixed candidates.
    assert [t for t in engine.TEMPLATES if t.dynamic is not None] == [template]


def test_plan_plans_the_scope_template_only_with_candidates(db):  # noqa: F811
    row = _strategy(db, "channel_breakout")
    lessons = [{"strategy_type": "channel_breakout", "cause": "signal_wrong", "count": 5, "contributing": {},
                "proposal": "p", "lesson": "l"}]
    plan = engine.plan_experiments(lessons, {"channel_breakout": row}, now=NOW, history=[], cooldown_days=28,
                                   max_experiments=3)
    assert plan["planned"] == []
    assert plan["untemplated"][0]["template"] == "scope" and "autopsy" in plan["untemplated"][0]["reason"]
    assert plan["skipped"] == [{"strategy_type": "channel_breakout", "template": "scope",
                                "reason": "no scope candidates in the latest signal autopsy"}]
    _autopsy_row(db, row, [RULE])
    plan = engine.plan_experiments(lessons, {"channel_breakout": row}, now=NOW, history=[], cooldown_days=28,
                                   max_experiments=3)
    assert [(p["strategy_type"], p["template"], p["trigger"]) for p in plan["planned"]] == [
        ("channel_breakout", "scope", "signal_wrong")]
    assert plan["untemplated"] == []


# -------------------------------------------------------------- the replay

def test_replay_ands_the_scope_condition_into_the_entries():
    frames = {"SPY": _walk(rows=400, seed=3, start="2024-01-02"), "AAPL": _walk(rows=400, seed=4, start="2024-01-02")}
    replays = {s: gate_replay.primary.replay_signals(f) for s, f in frames.items()}
    condition = {"f": "dow", "op": "<", "v": 3}
    assert "scope" not in gate_replay.setup_from_parameters({"scope": condition})   # the gate/exit setup is unchanged
    assert scope_condition({"scope": {"f": "bogus", "op": "<", "v": 1}}) is None
    open_ = gate_replay.simulate("channel_breakout", frames, frames["SPY"], {}, replays, calibration=None)
    scoped = gate_replay.simulate("channel_breakout", frames, frames["SPY"], {"scope": condition}, replays, calibration=None)
    # One position per symbol: dropping an entry can let a later one fire, so the scoped set is not a strict
    # subset; every scoped decision bar satisfies the condition and the open replay has bars that do not.
    assert 0 < len(scoped) and len(scoped) != len(open_)
    assert (pd.to_datetime(scoped["decision_date"]).dt.dayofweek < 3).all()
    invalid = gate_replay.simulate("channel_breakout", frames, frames["SPY"], {"scope": {"f": "bogus", "op": "<", "v": 1}},
                                   replays, calibration=None)
    assert len(invalid) == len(open_)                                            # an invalid scope is ignored
    assert any(pd.to_datetime(open_["decision_date"]).dt.dayofweek >= 3)
    mask = scope_mask(frames["AAPL"], condition)
    assert mask.dtype == bool and mask.sum() == (pd.to_datetime(frames["AAPL"]["date"]).dt.dayofweek < 3).sum()
    # The baseline the engine replays carries the row's scope, so a live scope is the baseline.
    setup = engine.current_setup({"scope": condition}, "channel_breakout")
    assert setup["scope"] == {"f": "dow", "op": "<", "v": 3.0}
    assert "scope" not in engine.current_setup({}, "channel_breakout")


# ------------------------------------------------------------ the live scope

class _Buyer:
    name, slug, parameters = "Buyer", "buyer", {}

    def generate_signal(self, symbol, data):
        return StrategySignalResult(action="BUY", probability_up=0.7, probability_down=0.3, confidence=0.7,
                                    reason="inner says buy", features={"k": 1})


class _Seller(_Buyer):
    def generate_signal(self, symbol, data):
        return StrategySignalResult(action="SELL", probability_up=0.3, probability_down=0.7, confidence=0.7,
                                    reason="inner says sell", features={})


def test_live_scope_turns_a_buy_into_a_hold_when_the_decision_bar_fails():
    frame = _walk(rows=120, seed=11, start="2026-01-02")
    last_rsi = float(rules.feature(frame, "rsi_2").iloc[-1])
    passing = apply_scope(_Buyer(), {"scope": {"f": "rsi_2", "op": "<", "v": last_rsi + 1}})
    failing = apply_scope(_Buyer(), {"scope": {"f": "rsi_2", "op": "<", "v": last_rsi - 1}})
    assert isinstance(passing, ScopedStrategy) and passing.name == "Buyer"   # attributes pass through
    ok = passing.generate_signal("AAPL", frame)
    assert ok.action == "BUY" and ok.features["k"] == 1
    assert ok.features["scope"]["allowed"] is True and ok.features["scope"]["value"] == pytest.approx(last_rsi, abs=1e-5)
    held = failing.generate_signal("AAPL", frame)
    assert held.action == "HOLD" and held.features["gated_action"] == "BUY" and held.features["scope"]["allowed"] is False
    assert held.reason.startswith("Entry scope: rsi_2 = ") and "inner says buy" in held.reason
    assert held.confidence == 0.7 and held.probability_up == 0.7                # only the action changes
    # SELLs and HOLDs are never touched; a value the frame cannot supply fails closed, flagged.
    assert apply_scope(_Seller(), {"scope": RULE}).generate_signal("AAPL", frame).action == "SELL"
    short = apply_scope(_Buyer(), {"scope": {"f": "dist_sma_200", "op": ">", "v": 0}}).generate_signal("AAPL", frame)
    assert short.action == "HOLD" and short.features["scope"]["reason"] == "scope_unavailable"
    # An invalid or missing scope leaves the strategy alone.
    assert apply_scope(_Buyer(), {}) .__class__ is _Buyer
    assert apply_scope(_Buyer(), {"scope": {"f": "rsi_2", "op": "<", "v": "ten"}}).__class__ is _Buyer
    assert apply_scope(_Buyer(), {"scope": {"f": "nope", "op": "<", "v": 1}}).__class__ is _Buyer
    assert apply_scope(_Buyer(), {"scope": "rsi_2 < 10"}).__class__ is _Buyer


def test_registry_applies_the_scope_to_every_strategy():
    frame = _walk(rows=300, seed=12, start="2025-01-02")
    scoped = get_strategy("channel_breakout", {"scope": {"f": "rsi_14", "op": ">", "v": 200}})  # can never hold
    assert isinstance(scoped, ScopedStrategy) and scoped.exit_policy() is not None
    result = scoped.generate_signal("AAPL", frame)
    assert result.action != "BUY"
    plain = get_strategy("channel_breakout", {})
    assert not isinstance(plain, ScopedStrategy)
    rule_strategy = get_strategy("rule_7", {"rule": {"entry": [{"f": "rsi_2", "op": "<", "v": 101}]}, "scope": RULE})
    assert isinstance(rule_strategy, ScopedStrategy)


# ---------------------------------------------------------------- adoption

def _fake_scope_replay(monkeypatch, scores):
    """``scores[label] = (expectancy, drawdown, trades)`` per scope label; the row without a scope is the baseline."""
    from app.services.strategies.scope import scope_label

    def simulate(strategy, frames, spy, setup, replays, vix=None, **extra):
        return pd.DataFrame({"label": [scope_label(setup.get("scope"))]})

    def window_rows(trades, frames):
        label = trades["label"].iloc[0]
        label = "baseline" if label == "off" else label
        spec = scores.get(label, scores["baseline"])
        return {w: {"trades": spec[2], "expectancy_bps": spec[0], "total_profit_bps": spec[2] * spec[0],
                    "portfolio_max_drawdown": spec[1]} for w in engine.WINDOWS}

    monkeypatch.setattr(gate_replay, "window_rows", window_rows)
    return simulate


def _signal_wrong_lesson(db, strategy_type, count=4):  # noqa: F811
    for i in range(count):
        _attribution(db, strategy_type=strategy_type, cause="signal_wrong", net_return=-0.02,
                     closed_at=NOW - timedelta(days=10 - i))
    _attribution(db, strategy_type=strategy_type, cause="edge", net_return=0.03, closed_at=NOW - timedelta(days=4))


def test_engine_adopts_a_scope_through_governance_and_the_live_path_honours_it(db, monkeypatch, small_universe):  # noqa: F811
    row = _strategy(db, "channel_breakout")
    _signal_wrong_lesson(db, "channel_breakout")
    _autopsy_row(db, row, [RULE, {"f": "dist_sma_200", "op": ">", "v": 0.0}])
    simulate = _fake_scope_replay(monkeypatch, {"baseline": (5.0, 0.20, 200), "rsi_2<10": (12.0, 0.15, 150),
                                                "dist_sma_200>0": (9.0, 0.15, 25)})   # too few trades
    result = engine.run_experiment_engine(db, now=NOW, simulate=simulate, **small_universe)
    assert result["status"] == "complete" and result["errors"] == []
    experiment = [e for e in result["experiments"] if e["template"] == "scope"][0]
    assert experiment["adopted"] == "rsi_2<10" and experiment["applied"] and experiment["trigger"] == "signal_wrong"
    payload = db.scalar(select(AuditLog).where(AuditLog.action == engine.ACTION_EXPERIMENT,
                                               AuditLog.entity_id == row.id)).payload
    assert set(payload["setups"]) == {"baseline", "rsi_2<10", "dist_sma_200>0"}   # off equals the baseline
    assert payload["candidate_keys"] == ["scope"] and "only 25 trades" in payload["decision"]["failed"]["dist_sma_200>0"]
    assert payload["decision"]["rule"] == engine.RULE
    db.refresh(row)
    assert row.parameters["scope"] == RULE
    governance = db.scalars(select(AuditLog).where(AuditLog.event_type == "strategy_governance",
                                                   AuditLog.action == "scope_update")).all()
    assert len(governance) == 1 and governance[0].entity_id == row.id
    assert governance[0].payload["scope"] == RULE and governance[0].payload["source"] == "experiment_engine"
    assert governance[0].payload["paper_only"] is True and "Sells and exits are unchanged" in governance[0].message
    # The seed-style override contract: a one-time seed step must find the engine's decision and leave it.
    assert autopsy.scope_decided_by_engine(db, row.id)
    # The live path builds a scoped strategy from the row's parameters.
    assert isinstance(get_strategy(row.strategy_type, row.parameters), ScopedStrategy)
    change = engine.adopted_changes(db)[0]
    assert change["template"] == "scope" and change["before"] == {"scope": None} and change["after"] == {"scope": RULE}
    assert change["description"] == "BUY only when rsi_2 < 10 on the decision bar"
    # The view shows the live scope.
    assert autopsy.signal_autopsy_view(db)["live_scopes"]["channel_breakout"]["condition"] == RULE
    ledger = engine.experiment_ledger(db)
    entry = [t for t in ledger["templates"] if t["name"] == "scope"][0]
    assert entry["dynamic"] is True and entry["candidates"] == [] and entry["kind"] == "scope"
    assert ledger["changes"][0]["template"] == "scope"
    # A revert removes the scope and records it under the same governance action.
    assert engine.revert_change(db, change, now=NOW + timedelta(days=30), reason="not helping")
    db.refresh(row)
    assert "scope" not in row.parameters and autopsy.scope_decided_by_engine(db, row.id)
    assert not isinstance(get_strategy(row.strategy_type, row.parameters), ScopedStrategy)


def test_engine_keeps_the_scope_when_nothing_beats_it(db, monkeypatch, small_universe):  # noqa: F811
    row = _strategy(db, "channel_breakout", {"scope": RULE})
    _signal_wrong_lesson(db, "channel_breakout")
    _autopsy_row(db, row, [{"f": "dist_sma_200", "op": ">", "v": 0.0}])
    simulate = _fake_scope_replay(monkeypatch, {"baseline": (5.0, 0.20, 200), "rsi_2<10": (12.0, 0.15, 150),
                                                "off": (5.0, 0.20, 200), "dist_sma_200>0": (11.0, 0.15, 150)})
    result = engine.run_experiment_engine(db, now=NOW, simulate=simulate, **small_universe)
    experiment = [e for e in result["experiments"] if e["template"] == "scope"][0]
    assert experiment["adopted"] is None
    db.refresh(row)
    assert row.parameters == {"scope": RULE}
    payload = db.scalar(select(AuditLog).where(AuditLog.action == engine.ACTION_EXPERIMENT)).payload
    assert set(payload["setups"]) == {"baseline", "off", "dist_sma_200>0"}     # the live scope is the baseline


# ------------------------------------------------------------------- wiring

def test_job_route_queue_and_beat_are_wired():
    from app.tasks import jobs

    beat = celery_app.conf.beat_schedule
    assert beat["signal-autopsy"]["task"] == "app.tasks.jobs.signal_autopsy_job"
    schedule = beat["signal-autopsy"]["schedule"]
    assert schedule.hour == {11} and schedule.minute == {30} and schedule.day_of_week == {0}   # Sunday 11:30 ET
    assert beat["experiment-engine"]["schedule"].hour == {12}                                  # before the engine
    assert celery_app.conf.task_routes["app.tasks.jobs.signal_autopsy_job"] == {"queue": "learning"}
    assert required_role("GET", "/stock-paper/signal-autopsy") == "viewer"
    assert callable(jobs.signal_autopsy_job)
