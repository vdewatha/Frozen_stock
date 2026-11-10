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


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run_watch(results, watch_seconds, reconcile=None):
    """Run main() in watch mode over scripted probe results."""
    clock = FakeClock()
    queue = list(results)
    calls = []

    def fake_probe():
        clock.now += 1
        return queue.pop(0) if queue else attempt()

    with patch.object(uptime, "WATCH_SECONDS", watch_seconds), \
         patch.object(uptime, "INTERVAL_SECONDS", 120), \
         patch.object(uptime, "RETRY_SECONDS", 120), \
         patch.object(uptime, "probe", side_effect=fake_probe), \
         patch.object(uptime, "reconcile_issues",
                      side_effect=reconcile or (lambda wanted, attempts, now: calls.append(wanted))), \
         patch.dict(uptime.os.environ, {"UPTIME_DRY_RUN": "0"}):
        assert uptime.main(sleep=clock.sleep, clock=clock) == 0
    return calls, clock


def test_single_check_without_watch():
    calls, _ = run_watch([attempt()], 0)
    assert calls == [{"down": False, "halted": False}]


def test_watch_reports_a_freeze_within_minutes_and_updates_issues_only_on_change():
    down = attempt(["startup:TimeoutError:None"])
    # Healthy, then frozen for two full rounds, then back.
    results = [attempt(), down, down, down, down, down, down, attempt()]
    calls, clock = run_watch(results, 3600)
    assert calls == [{"down": False, "halted": False}, {"down": True, "halted": False},
                     {"down": False, "halted": False}]
    assert clock.now <= 3600


def test_watch_stops_before_its_deadline_even_when_rounds_are_slow():
    down = attempt(["startup:TimeoutError:None"])
    _, clock = run_watch([down] * 200, 7200)
    assert clock.now <= 7200
    # The whole run, worst case, stays inside the 350-minute job timeout.
    assert 19800 + uptime.round_seconds() < 350 * 60


def test_failed_issue_update_is_retried_next_round():
    attempts_made = []

    def flaky(wanted, attempts, now):
        attempts_made.append(wanted)
        if len(attempts_made) == 1:
            raise uptime.subprocess.CalledProcessError(1, "gh", stderr="rate limited")

    run_watch([attempt(), attempt()], 600, reconcile=flaky)
    assert len(attempts_made) >= 2
