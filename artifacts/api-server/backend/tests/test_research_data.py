"""Research data snapshots: one versioned interface and a record of which data an experiment used."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.core.config import settings
from app.services import research_data as rd
from tests.test_experiment_engine import _adopt, small_universe  # noqa: F401  (helpers)
from tests.test_loss_feedback_loop import _strategy, db  # noqa: F401  (fixtures)

BACKEND = Path(__file__).resolve().parents[1]


def _bars(rows=300, start="2024-01-02", base=100.0, split_at=None):
    dates = pd.bdate_range(start, periods=rows).strftime("%Y-%m-%d")
    close = base + np.arange(rows) * 0.1
    adj = close.copy()
    if split_at is not None:  # a 2-for-1 split: raw prices halve, the adjusted close stays continuous
        adj[:split_at] = close[:split_at] / 2
    return pd.DataFrame({"date": dates, "open": close, "high": close + 0.5, "low": close - 0.5, "close": close,
                         "volume": 1_000_000, "adj_close": adj})


def _snapshot(root, *, with_terminal=False, with_events=True):
    securities = pd.DataFrame([
        {"security_id": "S1", "name": "Alpha Corp", "cik": "1", "ticker": "ALFA", "last_bar": "2025-02-24", "terminal_kind": "active",
         "terminal_return": ""},
        {"security_id": "S2", "name": "Beta Bank (old BBT)", "cik": "2", "ticker": "BBT", "last_bar": "2024-06-28",
         "terminal_kind": "acquired", "terminal_return": "0.04" if with_terminal else ""},
        {"security_id": "S3", "name": "Brookline (new BBT)", "cik": "3", "ticker": "BBT", "last_bar": "2025-02-24",
         "terminal_kind": "active", "terminal_return": ""},
    ])
    history = pd.DataFrame([
        {"security_id": "S1", "ticker": "ALF", "valid_from": "2024-01-02", "valid_to": "2024-05-31"},
        {"security_id": "S1", "ticker": "ALFA", "valid_from": "2024-06-01", "valid_to": ""},
        {"security_id": "S2", "ticker": "BBT", "valid_from": "2024-01-02", "valid_to": "2024-06-28"},
        {"security_id": "S3", "ticker": "BBT", "valid_from": "2024-09-03", "valid_to": ""},
    ])
    events = pd.DataFrame([
        {"security_id": "S1", "event_type": "earnings", "available_at": "2024-08-01T20:05:00Z", "detail": "8-K 2.02"},
        {"security_id": "S1", "event_type": "earnings", "available_at": "2024-05-02T12:01:00Z", "detail": "8-K 2.02"},
        {"security_id": "S2", "event_type": "merger", "available_at": "2024-03-11T11:00:00Z", "detail": "8-K 1.01"},
    ])
    return rd.write_canonical(
        root, name="test-universe", sources=["synthetic"], securities=securities,
        bars={"S1": _bars(split_at=100), "S2": _bars(rows=125, base=40.0), "S3": _bars(start="2024-09-03", base=12.0)},
        symbol_history=history, corporate_actions=pd.DataFrame([{"security_id": "S1", "date": "2024-05-21", "kind": "split", "value": "2"}]),
        events=events if with_events else None,
        exclusions=pd.DataFrame([{"scope": "OTC stocks", "reason": "not in the source"}]))


# ------------------------------------------------------------ the manifest

def test_the_snapshot_id_is_the_content_and_its_stated_meaning(tmp_path):
    first = _snapshot(tmp_path / "a")
    again = _snapshot(tmp_path / "b")
    assert first["snapshot_id"] == again["snapshot_id"] and len(first["snapshot_id"]) == 64  # same bytes, same id
    assert first["totals"]["files"] == 8 and first["layout"] == rd.CANONICAL_LAYOUT
    assert rd.load_manifest(tmp_path / "a")["snapshot_id"] == first["snapshot_id"]
    # A different bar, or the same bytes under a different contract, is a different snapshot.
    other = tmp_path / "c"
    _snapshot(other)
    frame = pd.read_csv(other / "bars" / "S2.csv.gz")
    frame.loc[0, "close"] = 41.0
    frame.to_csv(other / "bars" / "S2.csv.gz", index=False, compression={"method": "gzip", "mtime": 0})
    contract = rd.load_manifest(tmp_path / "a")["contract"]
    changed = rd.build_manifest(other, name="test-universe", contract=contract, sources=["synthetic"])
    assert changed["snapshot_id"] != first["snapshot_id"]
    restated = rd.build_manifest(tmp_path / "a", name="test-universe", sources=["synthetic"], contract={
        **contract, "terminal_payouts": {"status": "provided", "note": "claimed"}})
    assert restated["snapshot_id"] != first["snapshot_id"]
    # The creation time is information, not identity.
    stamped = rd.build_manifest(tmp_path / "a", name="test-universe", contract=contract, sources=["synthetic"],
                                created_at="2026-11-16T20:00:00+00:00")
    assert stamped["snapshot_id"] == first["snapshot_id"]


def test_verify_finds_a_changed_a_missing_and_an_extra_file(tmp_path):
    root = tmp_path / "snap"
    _snapshot(root)
    assert rd.verify(root, full=True) == {"snapshot_id": rd.load_manifest(root)["snapshot_id"], "ok": True, "missing": [],
                                          "extra": [], "changed": [], "hashes_checked": True, "files": 8}
    table = (root / "exclusions.csv").read_text()
    (root / "exclusions.csv").write_text(table.replace("OTC", "ADR"))  # same size, different bytes
    assert rd.verify(root)["ok"] is True                              # sizes alone do not see it
    assert rd.verify(root, full=True)["changed"] == ["exclusions.csv"]
    (root / "events.csv").unlink()
    (root / "notes.txt").write_text("added later")
    result = rd.verify(root, full=True)
    assert result["ok"] is False and result["missing"] == ["events.csv"] and result["extra"] == ["notes.txt"]


def test_a_manifest_must_state_every_part_and_cannot_be_edited_quietly(tmp_path):
    root = tmp_path / "snap"
    manifest = _snapshot(root)
    with pytest.raises(rd.SnapshotError, match="needs a status"):
        rd.build_manifest(root, name="x", sources=[], contract={k: v for k, v in manifest["contract"].items()
                                                              if k != "terminal_payouts"})
    with pytest.raises(rd.SnapshotError, match="needs a note"):
        rd.build_manifest(root, name="x", sources=[], contract={**manifest["contract"],
                                                              "daily_bars": {"status": "provided", "note": " "}})
    with pytest.raises(rd.SnapshotError, match="unknown contract part"):
        rd.build_manifest(root, name="x", sources=[], contract={**manifest["contract"], "extras": {"status": "provided", "note": "n"}})
    # Editing the manifest to claim more than it did breaks its id.
    edited = json.loads((root / rd.MANIFEST_NAME).read_text())
    edited["contract"]["terminal_payouts"] = {"status": "provided", "note": "delisting returns included"}
    (root / rd.MANIFEST_NAME).write_text(json.dumps(edited))
    with pytest.raises(rd.SnapshotError, match="does not match the manifest's content"):
        rd.load_manifest(root)
    with pytest.raises(rd.SnapshotError, match="has no SNAPSHOT.json"):
        rd.load_manifest(tmp_path)
    with pytest.raises(rd.SnapshotError, match="holds no data file"):
        (tmp_path / "empty").mkdir()
        rd.build_manifest(tmp_path / "empty", name="x", sources=[], contract=manifest["contract"])


# ----------------------------------------------------------- the interface

def test_identity_survives_a_reused_ticker_and_a_rename(tmp_path):
    _snapshot(tmp_path / "snap")
    snapshot = rd.open_snapshot(tmp_path / "snap")
    # BBT was one company until June 2024 and is another from September: the date decides, and a gap is nobody.
    assert snapshot.resolve("BBT", on="2024-03-01") == "S2" and snapshot.resolve("BBT", on="2025-01-15") == "S3"
    assert snapshot.resolve("BBT", on="2024-08-01") is None and snapshot.resolve("bbt") == "S3"
    assert snapshot.resolve("ALF", on="2024-02-01") == "S1" and snapshot.resolve("ALFA", on="2024-07-01") == "S1"
    assert snapshot.resolve("ALFA", on="2024-02-01") is None and snapshot.resolve("NOPE") is None
    assert list(snapshot.symbol_history("S1")["ticker"]) == ["ALF", "ALFA"]
    assert list(snapshot.securities()["security_id"]) == ["S1", "S2", "S3"]


def test_bars_are_raw_or_adjusted_and_the_adapter_feeds_the_app_s_replay(tmp_path):
    from app.services.strategy_discovery import load_frames

    _snapshot(tmp_path / "snap")
    snapshot = rd.open_snapshot(tmp_path / "snap")
    raw, adjusted = snapshot.bars("S1", adjusted=False), snapshot.bars("S1")
    assert raw["close"].iloc[0] == 100.0 and adjusted["close"].iloc[0] == 50.0   # before the split
    assert raw["close"].iloc[150] == adjusted["close"].iloc[150]                 # after it
    assert adjusted["open"].iloc[0] == pytest.approx(raw["open"].iloc[0] / 2)
    assert list(snapshot.corporate_actions("S1")["kind"]) == ["split"]
    with pytest.raises(rd.NotProvided, match="no bars for S9"):
        snapshot.bars("S9")
    # The same data through the app's own frame loader: BBT is the company that holds the ticker now.
    frames, missing = load_frames(["ALFA", "BBT", "GONE"], loader=snapshot.loader(), start="2024-01-01",
                                  earnings_loader=lambda symbol: pd.DataFrame(), news_loader=lambda symbol: None,
                                  sector_close=False)
    assert sorted(frames) == ["ALFA", "BBT"] and missing == ["GONE"]
    assert len(frames["ALFA"]) == 300 and frames["ALFA"]["close"].iloc[0] == 50.0
    assert frames["BBT"]["date"].iloc[0] == "2024-09-03" and frames["BBT"]["close"].iloc[0] == 12.0
    # A point-in-time loader serves the company that held the ticker then.
    old = snapshot.loader(on="2024-03-01")("BBT")
    assert old["close"].iloc[0] == 40.0 and len(old) == 125


def test_an_unknown_delisting_payout_is_reported_as_unknown_with_brackets_never_as_zero(tmp_path):
    _snapshot(tmp_path / "unknown")
    unknown = rd.open_snapshot(tmp_path / "unknown")
    assert unknown.status("terminal_payouts") == "absent"
    ended = unknown.terminal("S2")
    assert ended == {"security_id": "S2", "kind": "acquired", "last_bar": "2024-06-28", "terminal_return": None,
                     "known": False, "sensitivity": [0.0, -0.5, -1.0],
                     "note": "unknown: no delisting return: a delisted security's last bar is its last exchange trade"}
    assert unknown.terminal("S1")["kind"] == "active" and unknown.terminal("S1")["sensitivity"] is None
    with pytest.raises(rd.NotProvided, match="no security S9"):
        unknown.terminal("S9")
    _snapshot(tmp_path / "known", with_terminal=True)
    known = rd.open_snapshot(tmp_path / "known")
    assert known.status("terminal_payouts") == "provided"
    assert known.terminal("S2")["terminal_return"] == 0.04 and known.terminal("S2")["known"] is True
    assert known.snapshot_id != unknown.snapshot_id


def test_events_carry_when_they_became_available_and_a_missing_part_is_refused_not_emptied(tmp_path):
    _snapshot(tmp_path / "snap")
    snapshot = rd.open_snapshot(tmp_path / "snap")
    earnings = snapshot.events(security_id="S1", event_type="earnings")
    assert list(earnings["available_at"]) == ["2024-05-02T12:01:00Z", "2024-08-01T20:05:00Z"]  # in time order
    assert len(snapshot.events()) == 3 and list(snapshot.exclusions()["scope"]) == ["OTC stocks"]
    _snapshot(tmp_path / "bare", with_events=False)
    bare = rd.open_snapshot(tmp_path / "bare")
    assert bare.ref()["parts"]["event_availability"] == "absent"
    with pytest.raises(rd.NotProvided, match="does not provide event availability: no event table"):
        bare.events()


def test_a_legacy_cache_gets_an_id_but_its_files_stay_with_the_script_that_wrote_them(tmp_path):
    root = tmp_path / "legacy-cache"
    (root / "bars").mkdir(parents=True)
    (root / "bars" / "2026-01-02.pkl.gz").write_bytes(b"\x1f\x8bnot-read")
    (root / ".DS_Store").write_bytes(b"ignored")
    contract = {part: {"status": "partial", "note": "as the script left it"} for part in rd.PARTS}
    manifest = rd.build_manifest(root, name="legacy-cache", contract=contract, sources=["Alpaca"],
                                 layout="legacy:dip_rules_check.py")
    rd.write_manifest(root, manifest)
    assert [item["path"] for item in manifest["files"]] == ["bars/2026-01-02.pkl.gz"]
    snapshot = rd.open_snapshot(root)
    assert snapshot.ref()["layout"] == "legacy:dip_rules_check.py" and rd.verify(root, full=True)["ok"]
    with pytest.raises(rd.NotProvided, match="read its files with the script that wrote them"):
        snapshot.securities()


# -------------------------------------------------------------- the record

def test_snapshots_are_found_by_id_and_the_reference_is_what_an_experiment_records(tmp_path, monkeypatch):
    manifest = _snapshot(tmp_path / "one")
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / rd.MANIFEST_NAME).write_text("{}")
    monkeypatch.setattr(settings, "research_snapshot_root", str(tmp_path))
    assert rd.snapshots_root() == tmp_path
    listed = rd.list_snapshots()
    assert [item["name"] for item in listed] == ["test-universe"]            # the broken folder is skipped, not fatal
    ref = rd.dataset_ref(tmp_path / "one")
    assert ref == {"kind": "snapshot", "snapshot_id": manifest["snapshot_id"],
                   "manifest_sha256": rd.manifest_sha256(tmp_path / "one"), "name": "test-universe",
                   "layout": "canonical-v1", "parts": {
                       "security_identity": "provided", "symbol_changes": "provided", "daily_bars": "provided",
                       "corporate_actions": "provided", "terminal_payouts": "absent", "event_availability": "provided",
                       "coverage_exclusions": "provided"}}
    assert rd.open_snapshot(manifest["snapshot_id"][:12]).snapshot_id == manifest["snapshot_id"]
    assert rd.find_snapshot(manifest["snapshot_id"][:6]) is None            # too short to be an id
    with pytest.raises(rd.SnapshotError, match="no snapshot"):
        rd.open_snapshot("0" * 64)


def test_a_replay_records_the_snapshot_it_used_or_says_it_used_the_app_s_own_history(tmp_path, monkeypatch):
    manifest = _snapshot(tmp_path / "one")
    monkeypatch.setattr(settings, "research_snapshot_root", str(tmp_path))
    monkeypatch.setattr(settings, "research_snapshot_id", "")
    live, loader = rd.replay_dataset()
    assert loader is None and live["kind"] == "live_path" and live["snapshot_id"] is None
    assert live["parts"]["terminal_payouts"] == "absent" and any("survivorship" in gap for gap in live["known_gaps"])
    monkeypatch.setattr(settings, "research_snapshot_id", manifest["snapshot_id"])
    ref, loader = rd.replay_dataset()
    assert ref["snapshot_id"] == manifest["snapshot_id"] and loader("ALFA")["close"].iloc[0] == 50.0
    # A caller that injects its own loader is using that data, whatever is configured.
    assert rd.replay_dataset(injected=True)[0]["kind"] == "live_path"
    # A configured snapshot that cannot be opened is an error, never a silent switch to other data.
    monkeypatch.setattr(settings, "research_snapshot_id", "f" * 64)
    with pytest.raises(rd.SnapshotError):
        rd.replay_dataset()


def test_discovery_and_the_engine_refuse_to_run_on_other_data_when_the_snapshot_is_missing(db, monkeypatch):  # noqa: F811
    from app.services import experiment_engine, strategy_discovery

    monkeypatch.setattr(settings, "research_snapshot_id", "f" * 64)
    for blocked in (strategy_discovery.run_strategy_discovery(db), experiment_engine.run_experiment_engine(db)):
        assert blocked["status"] == "blocked" and blocked["reason"].startswith("research snapshot unavailable")


def test_every_engine_experiment_records_the_data_it_was_computed_on(db, monkeypatch, small_universe):  # noqa: F811
    from sqlalchemy import select

    from app.models import AuditLog
    from app.services import experiment_engine

    row = _strategy(db, "channel_breakout")
    _adopt(db, row, monkeypatch, small_universe)
    experiment = db.scalars(select(AuditLog).where(AuditLog.event_type == "experiment_engine",
                                                   AuditLog.action == "experiment")).one().payload
    run = db.scalars(select(AuditLog).where(AuditLog.event_type == "experiment_engine", AuditLog.action == "run")).one().payload
    # The tests inject their own frames, so the record says the app's own history path was used, and to when.
    assert experiment["dataset"]["kind"] == "live_path" and experiment["dataset"]["snapshot_id"] is None
    assert experiment["dataset"]["data_end"] is not None and experiment["dataset"]["universe"]["mode"] in ("today", "point_in_time")
    assert run["dataset"] == experiment["dataset"] and experiment_engine.experiment_history(db)[0]["dataset"]["name"] == "app_yfinance_history"


# ------------------------------------------------------------------ the tool

def test_the_command_line_tool_builds_verifies_and_lists(tmp_path):
    root = tmp_path / "spy-intraday-momentum-20261112"       # a preset name: its contract is used
    root.mkdir()
    (root / "daily_raw.csv.gz").write_bytes(b"\x1f\x8bdata")
    script = [sys.executable, str(BACKEND / "scripts" / "research_snapshot.py")]

    def run(*args):
        return subprocess.run([*script, *args], capture_output=True, text=True, cwd=BACKEND)

    built = run("build", str(root))
    assert built.returncode == 0, built.stderr
    out = json.loads(built.stdout)
    assert out["layout"] == "legacy:spy_intraday_momentum.py" and out["totals"] == {"files": 1, "bytes": 6}
    assert out["parts"]["security_identity"] == "provided" and out["parts"]["event_availability"] == "absent"
    assert json.loads(run("verify", str(root), "--full").stdout)["ok"] is True
    assert [item["snapshot_id"] for item in json.loads(run("list", "--root", str(tmp_path)).stdout)] == [out["snapshot_id"]]
    (root / "daily_raw.csv.gz").write_bytes(b"\x1f\x8bDATA")
    assert run("verify", str(root), "--full").returncode == 1
    # A folder that is not a preset needs a stated contract.
    other = tmp_path / "my-data"
    other.mkdir()
    (other / "x.csv").write_text("a\n1\n")
    refused = run("build", str(other))
    assert refused.returncode == 2 and "pass --contract" in refused.stderr
    assert "dip-rules-full-market-20261112" in run("presets").stdout
