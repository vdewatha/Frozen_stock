from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.live_safety import _current_data_gate, _monitoring_gate, _recovery_gate

NOW = datetime(2026, 9, 29, 18, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("age,expected", [(-1, "fail"), (0, "pass"), (300, "pass"), (301, "fail")])
@pytest.mark.parametrize("gate", [_current_data_gate, _monitoring_gate])
def test_monitoring_evidence_must_be_recent_and_not_from_the_future(gate, age, expected):
    snapshot = SimpleNamespace(id=1, generated_at=NOW - timedelta(seconds=age), status="clear",
                               checks=[{"metric": "intraday_feed_health", "status": "clear"}])
    db = SimpleNamespace(scalars=lambda query: SimpleNamespace(first=lambda: snapshot))
    assert gate(db, NOW)["status"] == expected


@pytest.mark.parametrize("heartbeat", ["last_monitor_heartbeat_at", "last_watchdog_heartbeat_at"])
@pytest.mark.parametrize("age,expected", [(None, "fail"), (-1, "fail"), (0, "pass"), (600, "pass"), (601, "fail")])
def test_recovery_requires_both_current_heartbeats(heartbeat, age, expected):
    state = SimpleNamespace(status="armed", accounting_review_required=False,
                            last_monitor_heartbeat_at=NOW, last_watchdog_heartbeat_at=NOW)
    setattr(state, heartbeat, None if age is None else NOW - timedelta(seconds=age))
    db = SimpleNamespace(get=lambda model, key: state)
    with patch("app.services.live_safety._now", return_value=NOW):
        assert _recovery_gate(db)["status"] == expected
