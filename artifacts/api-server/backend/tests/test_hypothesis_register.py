"""The hypothesis register: one view of what the research has established."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import Strategy
from app.services import forward_experiments as fx
from app.services import hypothesis_register as hr
from tests.test_forward_experiments import _ibs
from tests.test_loss_feedback_loop import db  # noqa: F401  (fixture)

UTC = timezone.utc
NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[4]


def _entry(view, entry_id):
    return next(entry for entry in view["entries"] if entry["id"] == entry_id)


def test_every_report_is_registered_or_listed_as_not_a_hypothesis():
    """A study cannot be written up in ``reports/`` without being added to the register."""
    register = hr.load_register()
    listed = {report for entry in register["entries"] for report in entry["sources"]["reports"]}
    other = set(register["not_hypotheses"])
    on_disk = {f"reports/{path.name}" for path in (REPO / "reports").glob("*.md")}
    assert on_disk, "reports/ was not found from the test"
    assert sorted(on_disk - listed - other) == [], "add each new report to hypothesis-register.json"
    assert sorted((listed | other) - on_disk) == [], "the register names a report that no longer exists"
    assert listed.isdisjoint(other)
    for entry in register["entries"]:
        notes = entry["sources"].get("notes")
        assert notes is None or (REPO / notes).is_dir(), entry["id"]


def test_the_packaged_register_is_well_formed_and_a_bad_one_is_refused(tmp_path):
    register = hr.load_register()
    assert register["statuses"] == list(hr.STATUSES) and len(register["entries"]) >= 30
    for entry in register["entries"]:
        assert datetime.fromisoformat(entry["decided_on"]) and entry["revisit_if"] and entry["why"]
    base = register["entries"][0]
    for broken, message in (
        ({**base, "status": "promising"}, "unknown status"),
        ({key: value for key, value in base.items() if key != "revisit_if"}, "is missing revisit_if"),
        ({**base, "sources": {"reports": []}}, "names no report"),
    ):
        path = tmp_path / "register.json"
        path.write_text(json.dumps({"entries": [broken]}))
        with pytest.raises(ValueError, match=message):
            hr.load_register(path)
    path.write_text(json.dumps({"entries": [base, base]}))
    with pytest.raises(ValueError, match="used twice"):
        hr.load_register(path)


def test_a_study_on_a_snapshot_names_it_by_id_and_the_view_passes_it_on(db):  # noqa: F811
    from scripts.research_snapshot import PRESETS

    register = hr.load_register()
    named = {entry["id"]: entry["dataset"] for entry in register["entries"] if entry.get("dataset")}
    assert set(named) == {"dip-rules-full-market", "new-high-breakout-full-market", "earnings-drift", "filings-8k",
                          "house-disclosed-purchases", "news-rules", "spy-intraday-momentum", "stocks-in-play-breakout"}
    for dataset in named.values():
        assert dataset["note"].startswith("Snapshot ids were assigned on 2026-11-16, after the study ran")
        for snapshot in dataset["snapshots"]:
            assert len(snapshot["snapshot_id"]) == 64 and snapshot["name"] in PRESETS
    # The news study reads its events from one cache and its prices from another.
    assert [s["name"] for s in named["news-rules"]["snapshots"]] == ["public-information-20261114",
                                                                      "dip-rules-full-market-20261112"]
    view = hr.hypothesis_register(db, now=NOW)
    assert _entry(view, "earnings-drift")["dataset"] == named["earnings-drift"]
    assert _entry(view, "vix-gate")["dataset"] is None  # computed on the app's own history: no snapshot


def test_closed_families_are_the_ones_where_every_study_failed():
    closed = hr.closed_families()
    for family in ("post_earnings_drift", "insider_trades", "public_information", "intraday", "breakout", "dip_buying",
                   "gap_fill", "factor_tilt", "news"):
        assert family in closed, family
    assert closed["intraday"] == ["spy-intraday-momentum", "stocks-in-play-breakout"]
    # A family with an adopted or unsettled study is not closed.
    assert "market_timing_gates" not in closed and "meta_label" not in closed
    mixed = {"entries": [{"family": "f", "status": "failed", "id": "a"}, {"family": "f", "status": "passed", "id": "b"},
                         {"family": "g", "status": "failed", "id": "c"}]}
    assert hr.closed_families(mixed) == {"g": ["c"]}


def test_the_view_says_what_was_tested_and_whether_each_hypothesis_can_trade(db, monkeypatch):  # noqa: F811
    from app.models.stock_paper import StockPaperStrategyEvidence

    monkeypatch.setattr(settings, "paper_exploration_enabled", True)
    _ibs(db)
    fx.register_forward_experiments(db, now=NOW)
    breakout = Strategy(name="Channel Breakout", strategy_type="channel_breakout", parameters={}, is_active=True,
                        current_status="paper_trading_active")
    paused = Strategy(name="Sector reversal", strategy_type="sector_reversal", parameters={}, is_active=True,
                      current_status="paused")
    db.add_all([breakout, paused])
    db.flush()
    view = hr.hypothesis_register(db, now=NOW)
    assert view["paper_only"] is True and view["unavailable"] == []
    assert sum(view["counts"].values()) == len(view["entries"]) and view["counts"]["collecting"] == 6
    # A study with no strategy row is research only.
    drift = _entry(view, "earnings-drift")
    assert drift["status"] == "failed" and drift["source"] == "report" and drift["sources"]["pr"] == 92
    assert drift["can_place_paper_orders"] is False and drift["paper_orders"]["detail"] == "research only: no strategy row"
    # Shadow, paused and unevidenced rows cannot trade; the reason is stated per row.
    reversion = _entry(view, "short-horizon-reversion")
    assert reversion["can_place_paper_orders"] is False
    assert {row["strategy_type"]: row["detail"] for row in reversion["paper_orders"]["rows"]} == {
        "ibs_mean_reversion": "shadow: signals recorded, no orders", "rsi2_pullback": "no strategy row"}
    assert _entry(view, "sector-reversal")["paper_orders"]["rows"][0]["detail"] == "status paused: no entries"
    gate = _entry(view, "vix-gate")
    assert gate["status"] == "passed" and gate["caveat"].startswith("In-sample")
    assert gate["can_place_paper_orders"] is False and "no approved forward evidence" in gate["paper_orders"]["detail"]
    db.add(StockPaperStrategyEvidence(strategy_id=breakout.id, evidence_id="e-1", status="approved",
                                      verified_drawdown=Decimal("0.01"), consecutive_losses=0, observed_at=NOW,
                                      expires_at=NOW + timedelta(days=1), provenance={"kind": "forward_evidence"}))
    db.flush()
    assert _entry(hr.hypothesis_register(db, now=NOW), "vix-gate")["can_place_paper_orders"] is True
    assert _entry(hr.hypothesis_register(db, now=NOW + timedelta(days=2)), "vix-gate")["can_place_paper_orders"] is False
    # Forward experiments: collecting, with the sample they need and how long it takes.
    two = _entry(view, "forward-experiment-2")
    assert two["status"] == "collecting" and two["source"] == "forward_experiment" and two["family"] == "weekly_reversal"
    assert two["sample"] == {"required": 800, "observed": None, "expected_trades_per_year": 45, "years_to_verdict": 17.8}
    assert two["next_decision"]["date"] == "2027-02-10" and "about 17.8 years" in two["why"]
    assert two["can_place_paper_orders"] is True and two["paper_orders"]["detail"] == "learning-sized exploration entries only"
    six = _entry(view, "forward-experiment-6")
    assert six["can_place_paper_orders"] is False and six["paper_orders"]["detail"].startswith("tag-and-compare on ibs_mean_reversion")


def test_a_closed_forward_experiment_reads_as_decided_and_cannot_trade(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "paper_exploration_enabled", True)
    _ibs(db)
    fx.register_forward_experiments(db, now=NOW)
    registrations = {int(r["candidate"]): r for r in fx.registrations(db)}
    fx.close_candidate(db, registrations[2], reason_kind="stop_rule", reasons=["clustered t -2.40 <= -2 after 61 trades"],
                       now=NOW, actor="forward_experiments_review", review_number=1)
    fx.close_candidate_by_number(db, 1, reason="cannot reach a verdict in a useful time", now=NOW)
    view = hr.hypothesis_register(db, now=NOW)
    two, one, three = (_entry(view, f"forward-experiment-{n}") for n in (2, 1, 3))
    assert two["status"] == "failed" and two["result"] == "closed 2026-11-16 (stop rule)"
    assert two["why"] == "clustered t -2.40 <= -2 after 61 trades" and two["next_decision"] is None
    assert two["can_place_paper_orders"] is False and "forward experiment 2 is closed" in two["paper_orders"]["detail"]
    # An operator closure is a closure without a verdict.
    assert one["status"] == "inconclusive" and one["can_place_paper_orders"] is False
    assert three["status"] == "collecting" and three["can_place_paper_orders"] is True
    assert view["counts"]["collecting"] == 4


def test_live_sources_are_summarised_and_a_failing_one_never_empties_the_register(db, monkeypatch):  # noqa: F811
    from app.services.audit import write_audit_log

    assert [e["id"] for e in hr.hypothesis_register(db, now=NOW)["entries"] if e["source"] != "report"] == []
    write_audit_log(db, event_type="strategy_discovery", action="run", status="complete", message="run",
                    payload={"proven": 0, "explore": 2, "library": 342, "trials": 3104, "data_end": "2026-11-06"})
    write_audit_log(db, event_type="experiment_engine", action="change_adopted", status="adopted", message="c1",
                    payload={"change_id": "c1", "strategy_type": "channel_breakout"})
    write_audit_log(db, event_type="experiment_engine", action="change_verdict", status="reverted", message="c1",
                    payload={"change_id": "c1", "state": "reverted", "verdict": "not_helping"})
    db.flush()
    view = hr.hypothesis_register(db, now=NOW)
    library = _entry(view, "discovery-library")
    assert library["status"] == "collecting" and library["result"] == "0 proven, 2 explore of 342 candidates; 3104 trials ever"
    engine = _entry(view, "learning-engine-changes")
    assert engine["status"] == "collecting" and engine["result"] == "1 reverted of 1 adopted"
    monkeypatch.setattr(hr, "_discovery_entry", lambda _db: (_ for _ in ()).throw(RuntimeError("boom")))
    view = hr.hypothesis_register(db, now=NOW)
    assert view["unavailable"] == [{"source": "strategy_discovery", "error": "RuntimeError: boom"}]
    assert len([e for e in view["entries"] if e["source"] == "report"]) == len(hr.load_register()["entries"])


def test_the_route_exists_for_viewers():
    from app.api.stock_paper import router
    from app.core.security import required_role

    paths = {route.path: route.methods for route in router.routes}
    assert "GET" in paths["/stock-paper/hypothesis-register"]
    assert required_role("GET", "/stock-paper/hypothesis-register") == "viewer"
