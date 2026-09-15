from datetime import datetime, timezone
from types import SimpleNamespace

from scripts.report_autonomous_paper_soak import (
    _forward_evidence_status,
    _interruption_matrix_status,
    _parse_interruption,
    _parse_since,
)


def test_soak_report_requires_explicit_utc_interruption_shape():
    assert _parse_interruption("redis=recovered:coordination resumed") == {
        "name": "redis",
        "status": "recovered",
        "reason": "coordination resumed",
    }


def test_soak_report_rejects_malformed_interruption():
    try:
        _parse_interruption("worker")
    except ValueError as exc:
        assert "NAME=STATUS:REASON" in str(exc)
    else:
        raise AssertionError("malformed interruption was accepted")


def test_soak_report_rejects_interruption_outside_controlled_matrix():
    try:
        _parse_interruption("database=blocked:database unavailable")
    except ValueError as exc:
        assert "unknown interruption" in str(exc)
    else:
        raise AssertionError("unknown interruption was accepted")


def test_soak_report_requires_every_controlled_interruption():
    result = _interruption_matrix_status(
        [
            {"name": "feed", "status": "deferred", "reason": "stale"},
            {"name": "ledger", "status": "blocked", "reason": "residual"},
        ]
    )
    assert result["complete"] is False
    assert result["missing"] == ["worker", "beat_lease", "redis"]


def test_soak_report_keeps_forward_evidence_incomplete_without_reports():
    result = _forward_evidence_status([])
    assert result["complete"] is False
    assert "No cycle-owned" in result["reason"]


def test_soak_report_rejects_passing_report_without_frozen_session_evidence():
    report = SimpleNamespace(
        id=7,
        trial_id="trial-1",
        decision="pass",
        gates={"regular_sessions": {"status": "pass"}},
        evidence={},
        policy={"regular_sessions": 20, "minimum_decision_coverage": "0.90"},
    )
    result = _forward_evidence_status([report])
    assert result["complete"] is False
    assert result["passing_reports"] == 0
    assert "frozen evidence" in result["reason"]


def test_soak_report_normalizes_naive_since_timestamp():
    result = _parse_since("2026-09-14T12:00:00")
    assert result == datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
