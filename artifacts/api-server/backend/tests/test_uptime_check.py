"""Alert rules of the outside uptime check in .github/scripts/uptime_check.py."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / ".github" / "scripts" / "uptime_check.py"
spec = importlib.util.spec_from_file_location("uptime_check", SCRIPT)
uptime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uptime)

MARKET = datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc)  # 11:00 ET Tuesday
EVENING = datetime(2026, 11, 10, 23, 0, tzinfo=timezone.utc)  # 18:00 ET


def attempt(problems=(), **heartbeat):
    return {"startup_code": 200, "startup_status": "ready", "heartbeat_code": 200 if not problems else 503,
            "heartbeat": {"tradable": True, **heartbeat}, "problems": list(problems)}


def test_one_failed_try_then_pass_is_not_down():
    assert uptime.evaluate([attempt(["heartbeat:x"]), attempt()], MARKET) == {"down": False, "halted": False}


def test_every_try_failing_is_down():
    tries = [attempt(["startup:None:None"])] * 3
    assert uptime.evaluate(tries, EVENING)["down"] is True


def test_long_halt_alerts_only_in_market_hours():
    halted = [attempt(tradable=False, halted_for_seconds=3600)]
    assert uptime.evaluate(halted, MARKET)["halted"] is True
    assert uptime.evaluate(halted, EVENING)["halted"] is False
    short = [attempt(tradable=False, halted_for_seconds=600)]
    assert uptime.evaluate(short, MARKET)["halted"] is False


@pytest.mark.parametrize("code,body,expected", [
    (404, None, []),  # deployment predates the heartbeat route
    (401, {"detail": "Authenticated identity required"}, []),
    (503, {"problems": ["no_beat_lease"]}, ["heartbeat:no_beat_lease"]),
    (None, None, ["heartbeat:TimeoutError"]),
])
def test_probe_problems(code, body, expected):
    responses = {"/api/startup": (200, {"status": "ready"}, None),
                 "/api/heartbeat": (code, body, "TimeoutError" if code is None else None)}
    with patch.object(uptime, "fetch", side_effect=lambda path: responses[path]):
        assert uptime.probe()["problems"] == expected


def test_startup_not_ready_skips_heartbeat():
    calls = []

    def fake(path):
        calls.append(path)
        return 503, {"status": "starting"}, None

    with patch.object(uptime, "fetch", side_effect=fake):
        result = uptime.probe()
    assert calls == ["/api/startup"] and result["problems"] == ["startup:503:starting"]


def test_issues_open_once_and_close_on_recovery():
    commands = []
    open_numbers = {"down": None, "halted": None}

    def fake_gh(*args):
        commands.append(args[:2])
        return "[]"

    with patch.object(uptime, "gh", side_effect=fake_gh), \
         patch.object(uptime, "open_issue", side_effect=lambda kind: open_numbers[kind]):
        uptime.reconcile_issues({"down": True, "halted": False}, [attempt(["x"])], MARKET)
        assert ("issue", "create") in commands
        commands.clear()
        open_numbers["down"] = 7
        uptime.reconcile_issues({"down": True, "halted": False}, [attempt(["x"])], MARKET)
        assert commands == []  # already open: no duplicate, no comment spam
        uptime.reconcile_issues({"down": False, "halted": False}, [attempt()], MARKET)
        assert commands == [("issue", "close")]
