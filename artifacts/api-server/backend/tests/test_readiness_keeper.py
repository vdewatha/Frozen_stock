"""2026-11-10: public /ready served a boot-time "blocked" snapshot for 16 minutes.

The snapshot refreshed only on request and one build takes minutes on the
hosted VM, so callers after a quiet spell saw stale evidence; failed refreshes
left no trace. A keeper now refreshes it on an interval and refreshes log.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from app.api import routes


def test_keeper_starts_once_and_keeps_refreshing():
    started = []

    class FakeThread:
        def __init__(self, target, name, daemon):
            started.append(name)
            self.target = target

        def start(self):
            pass

    with patch.object(routes, "_readiness_keeper_started", False), \
            patch.object(routes.threading, "Thread", FakeThread):
        assert routes.start_readiness_keeper(1) is True
        assert routes.start_readiness_keeper(1) is False
    assert started == ["readiness-keeper"]


def test_keeper_loop_survives_errors():
    calls = []

    class Stop(Exception):
        pass

    def fake_sleep(_):
        if len(calls) >= 2:
            raise Stop

    captured = {}

    class FakeThread:
        def __init__(self, target, name, daemon):
            captured["target"] = target

        def start(self):
            pass

    with patch.object(routes, "_readiness_keeper_started", False), \
            patch.object(routes.threading, "Thread", FakeThread), \
            patch.object(routes, "system_readiness", side_effect=lambda: calls.append(1) or (_ for _ in ()).throw(RuntimeError("x"))), \
            patch.object(routes.time, "sleep", side_effect=fake_sleep):
        routes.start_readiness_keeper(1)
        try:
            captured["target"]()
        except Stop:
            pass
    assert len(calls) == 2


def test_failed_refresh_is_logged_and_keeps_the_error(capsys):
    session = MagicMock()
    with patch("app.db.session.SessionLocal", return_value=session), \
            patch.object(routes, "readiness_snapshot", side_effect=TimeoutError("db slow")), \
            patch.object(routes, "_readiness_refresh_generation", 5), \
            patch.object(routes, "_readiness_refresh_error", None):
        routes._refresh_readiness_snapshot(5)
        assert routes._readiness_refresh_error == "TimeoutError"
    assert "readiness refresh failed" in capsys.readouterr().out
    session.close.assert_called_once()


def test_public_ready_reports_snapshot_age():
    old = {"overall_status": "ready", "generated_at": datetime.utcnow() - timedelta(seconds=90), "checks": []}
    with patch.object(routes, "system_readiness", return_value=old):
        body = routes.ready()
    assert 85 <= body["age_seconds"] <= 120
