"""Market-hours deploy guard used as the first Replit deployment build step."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import subprocess
import sys
import tomllib
from unittest.mock import patch

import pytest

BACKEND = Path(__file__).parents[1]
REPO = BACKEND.parents[2]
SCRIPT = BACKEND / "scripts" / "deploy_window_guard.py"
spec = importlib.util.spec_from_file_location("deploy_window_guard", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

EDT = timezone(timedelta(hours=-4))
EST = timezone(timedelta(hours=-5))


@pytest.mark.parametrize("moment,blocked", [
    (datetime(2026, 10, 29, 9, 24, 59, tzinfo=EDT), False),  # Thursday, just before
    (datetime(2026, 10, 29, 9, 25, tzinfo=EDT), True),
    (datetime(2026, 10, 29, 12, 0, tzinfo=EDT), True),
    (datetime(2026, 10, 29, 16, 4, 59, tzinfo=EDT), True),
    (datetime(2026, 10, 29, 16, 5, tzinfo=EDT), False),
    (datetime(2026, 10, 31, 12, 0, tzinfo=EDT), False),  # Saturday
    (datetime(2026, 11, 26, 12, 0, tzinfo=EST), False),  # Thanksgiving
    (datetime(2026, 12, 25, 12, 0, tzinfo=EST), False),  # Christmas
    (datetime(2026, 4, 3, 12, 0, tzinfo=EDT), False),  # Good Friday
    (datetime(2026, 7, 3, 12, 0, tzinfo=EDT), False),  # Independence Day observed
    (datetime(2026, 11, 27, 12, 0, tzinfo=EST), True),  # early close day still blocked
    # UTC input is converted to New York time (10:00 EST in winter).
    (datetime(2026, 12, 1, 15, 0, tzinfo=timezone.utc), True),
    (datetime(2026, 12, 1, 21, 6, tzinfo=timezone.utc), False),
])
def test_window(moment, blocked):
    assert guard.in_blocked_window(moment) is blocked
    code, message = guard.check(moment, environ={})
    assert code == (1 if blocked else 0)
    if blocked:
        assert "refusing to deploy" in message and guard.OVERRIDE_ENV in message


def test_override_allows_market_hours_deploy():
    moment = datetime(2026, 10, 29, 12, 0, tzinfo=EDT)
    assert guard.check(moment, environ={guard.OVERRIDE_ENV: "true"})[0] == 0
    assert guard.check(moment, environ={guard.OVERRIDE_ENV: "TRUE "})[0] == 0
    assert guard.check(moment, environ={guard.OVERRIDE_ENV: "1"})[0] == 1
    assert guard.check(moment, environ={guard.OVERRIDE_ENV: "false"})[0] == 1


def test_holiday_rules_match_the_exchange_calendar():
    from app.services.exchange_sessions import nyse_holidays

    for year in range(2026, 2036):
        assert guard.nyse_holidays(year) == nyse_holidays(year), year


def test_saturday_new_year_is_not_observed_on_friday():
    assert date(2027, 12, 31) not in guard.nyse_holidays(2027)
    assert date(2028, 1, 1) not in guard.nyse_holidays(2028)


def test_fallback_eastern_conversion_without_tzdata_matches_zoneinfo():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with patch.object(guard, "_eastern_tz", return_value=None):
        fallback = [guard.to_eastern(start + timedelta(hours=7 * step)) for step in range(1300)]
    real = [guard.to_eastern(start + timedelta(hours=7 * step)) for step in range(1300)]
    assert [value.replace(tzinfo=None) for value in fallback] == [value.replace(tzinfo=None) for value in real]


def test_script_is_stdlib_only_and_exit_codes():
    source = SCRIPT.read_text()
    assert "import app" not in source and "from app" not in source
    env = {"PATH": "/usr/bin:/bin"}
    blocked = subprocess.run([sys.executable, "-I", str(SCRIPT), "--now", "2026-10-29T12:00:00-04:00"],
                             capture_output=True, text=True, env=env, cwd="/")
    assert blocked.returncode == 1 and "refusing to deploy" in blocked.stderr
    allowed = subprocess.run([sys.executable, "-I", str(SCRIPT), "--now", "2026-10-29T20:00:00-04:00"],
                             capture_output=True, text=True, env=env, cwd="/")
    assert allowed.returncode == 0
    override = subprocess.run([sys.executable, "-I", str(SCRIPT), "--now", "2026-10-29T12:00:00-04:00"],
                              capture_output=True, text=True, env=env | {guard.OVERRIDE_ENV: "true"}, cwd="/")
    assert override.returncode == 0


def test_guard_is_the_first_deployment_build_step():
    config = tomllib.loads((REPO / ".replit").read_text())
    build = config["deployment"]["build"]
    assert build.startswith("python3 artifacts/api-server/backend/scripts/deploy_window_guard.py && ")
    assert "pnpm --filter @workspace/api-server build" in build
